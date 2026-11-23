# ADR 0008 — Git & GitHub information: the repository-provenance layer

Status: **Accepted; initial implementation in progress.** Date: 2026-11-22.

Builds on ADR 0007 (image-keyed deployment tier), ADR 0004 (external references as upgradable
placeholder nodes), ADR 0002/0003 (incremental resync + triggers), and the
[cross-ingestor schema contract](../graph-model.md).

## Context

The graph models **code structure** (per-language) and, since ADR 0007, the **deployment tier**
(code → image → service). It does not model the **provenance** around the code: how artifacts are
built/shipped, who wrote and owns it, and the process that changed it. That is "git & GitHub
information" — three axes with different sources, ingestion modes, costs, and value:

| Axis | Answers | Source | Mode | Auth? |
|---|---|---|---|---|
| **A. CI** | which checks passed or failed for a commit? | public GitHub Checks API | paged API (check suites/runs) | no (public repos) |
| **B. History / ownership** | who wrote / last touched / how hot is this? | `git log` (local) | append-by-commit | no |
| **C. GitHub platform** | which PR/issue/review changed this; who owns this path? | public GitHub API | networked, paged | no (public repos only) |

The decisions below are driven by five concerns the reviewer raised: **(1)** how we connect to the
remote to pull PRs/issues, **(2)** keeping this *out* of the branch-/commit-switched code sync,
**(3)** which fields to index, **(4)** webhooks vs polling, **(5)** whether to track CI — all under a
**memory-vs-value** lens and "how the user sets it up".

## The architectural decision: provenance is a SEPARATE subgraph with its own lifecycle

This is the spine that answers concern **(2)** and shapes the rest.

- **Separate labels.** Provenance nodes (`GitCommit`, `GitUser`, `GitPullRequest`, `GitIssue`,
  `GitRelease`, …) use labels that are **not in any code engine's `OWNED_LABELS`**. The code's scoped
  incremental resync (`stream_apply(proj_id, <code labels>, …)`, ADR 0002/0005) is label-scoped, so
  code delta application never deletes provenance nodes.
- **Separate ownership, shared opt-in.** Code triggers never own or delete provenance labels. When
  `git_info.enabled` is on, ingestion refreshes local provenance and checks remote metadata no more
  than daily; `auryn git-info sync` forces a refresh. With the setting off, code ingestion does not
  collect Git information.
- **Repo-scoped keys, not commit-scoped.** PRs/issues/commits key on the **repo + their own id**
  (`github://<owner>/<repo>/pull/<n>`, `git://<repo>/<sha>`), so they persist across checkouts.
- **Joins tolerate code churn.** Provenance→code edges reference code by **stable key** (file path,
  symbol `linkage_key`/qname, commit SHA). When the current branch lacks a referenced `File`/`Function`,
  the edge simply has no in-branch target (or targets an ADR-0004-style placeholder that *heals* when
  the code node exists). Neither sync deletes the other's nodes.
- **One exception — CI *config* is code.** Workflow YAML lives in the checkout, so it is **branch-tied
  source**: parsed by the code/deployment-tier sync, not the provenance sync. Only CI *runs* are
  provenance. (See §A.)

## Policy: public repos only, opt-in

To avoid data leakage, **Auryn never collects Git information from a private or unverified repo.**
Collection is opt-in through the install-time setting `git_info.enabled` and only runs for a
repository whose GitHub visibility can be verified as public:
- **Via the local `gh` token when present, else the anonymous REST API** — a token is used only to
  raise the rate limit and reads only public data; none is required (see "(1) Connecting to GitHub").
- **Public-repo gate:** before any content pull, check visibility (`GET /repos/{owner}/{repo}`) and
  **refuse a private repo** (log why). Public data carries no
  leakage risk.
- When enabled, ingestion collects local Git data and refreshes remote data at most daily. An explicit
  `auryn git-info sync` forces a remote refresh.

## Automatic? Only after opt-in

With the setting off, ingestion makes no Git-information API calls. With it on, code ingestion also
refreshes the provenance graph; remote issue/PR/release/tag enumeration is throttled to once per day.
`auryn git-info sync` is available for an immediate refresh. The collector uses the local `gh` token
when one is present (only to raise the rate limit — it reads only public data) and the anonymous public
API otherwise, and first checks that the repository is public; private or unverified repositories
contribute no Git information nodes.

## (1) Connecting to GitHub

- Identify `(owner, repo)` from the origin URL.
- Prefer the local **`gh` CLI token, fetched once and reused** for every REST and GraphQL call this
  process (the authenticated 5000/hr rate limit, and no `gh` subprocess per request); fall back to the
  **anonymous** public REST API (60/hr) when `gh` is not logged in. Only public data is read either way.
- Verify public visibility before reading local history or remote contents; failure skips the entire
  Git-information layer without failing code ingestion.

## (2′) Keeping provenance out of the code sync — summary

Covered by the separate-subgraph spine above: distinct labels (not resync-scoped), repo-scoped ids,
and churn-tolerant joins. A code sync may trigger a configured provenance refresh, but code delta
calculation cannot delete or replace provenance nodes.

## (3) Which fields to index — and memory

Follow the schema.py rule: **index only join/lookup keys and `WHERE`-clause fields; store the rest as
unindexed properties.** High-cardinality free text (PR/issue titles & bodies, commit messages) is
**not** indexed unless a feature needs full-text over it — and a full-text index is a real memory cost
(ADR 0007/embeddings), so it's opt-in.

| Node | Indexed | Stored (unindexed) |
|---|---|---|
| `Commit` | `sha` | message, date, insertions/deletions |
| `GitUser` | `identity` — a commit's git email, OR `github:<login>` when the commit's email links to a GitHub account (unifies commit authors with PR/issue/release authors — one node per person) | display name |
| `PullRequest`/`Issue` | `number`, `merge_commit_sha`/`head_sha` (join to `Commit`) | title, state, labels |
| `CIRun` | `commit_sha`, image ref via the shared `image://` key | status, started_at |

**Memory is the dominant design lever for history (B).** A busy repo has 10⁵+ commits; a full
`Commit` + `CHANGED`-edge graph can **dwarf the code graph**.

> **Status (not yet implemented):** the current collector always walks the full commit history (an
> `--all` walk) into `GitCommit`/`GitUser` nodes with `CHANGED` edges. The ownership-rollup default and
> the bounded (last-N / since-date) opt-in described below are the **intended** memory design, tracked
> as follow-up work — they do not yet ship. Until then, a very large repo pays full-history storage.

So B is intended to default to an **ownership ROLLUP**,
not a history graph: aggregate `git log` into **properties on the existing `File`/`Function` nodes** —
`last_author`, `last_changed`, `commit_count`/`churn`, `author_count`. That delivers ~all the *value*
(ownership, recency, hotspots, reviewer suggestion) at a **tiny fraction of the memory** (no node/edge
explosion, nothing new to index — the props are read on an already-located node). The full
`Commit`/`Contributor` graph is **opt-in and bounded** (last-N / since-date) for users who want
commit-level traversal. History is immutable → the rollup/append sync is cheap and incremental-by-SHA.

## (4) Webhooks vs polling

**Polling, not webhooks, for v1.** Webhooks need a **public HTTPS endpoint** to receive GitHub's POSTs
plus per-repo webhook setup and secret handling — infrastructure a **local-first** tool doesn't have
and shouldn't require a user to stand up. Instead: **on-demand + optional scheduled pull**. On-demand is
the code-triggered refresh (throttled daily) and the manual `auryn git-info sync`. The scheduled pull is
**`auryn git-info watch [path] --interval <seconds>`**: a foreground loop that re-runs a single-flight,
force-remote sync every interval, so a repo whose *code* is static still picks up new issues/PRs/releases/
CI. It is run under a process manager (systemd, nohup, a container) alongside the ingest client — no
webhook endpoint required. The remaining cost lever is **conditional requests (ETag / `If-Modified-Since`)
and `since`/cursor pagination** so a poll that finds nothing new is nearly free (follow-up). Webhooks
remain a reasonable *later* option for a **hosted/server** deployment (push-based, real-time) — not the
default.

## (5) Do we track CI?

**Yes to public Checks API data, modeled provider-neutrally.** `GitCheckSuite` and `GitCheckRun`
represent checks from GitHub Actions and third-party GitHub Apps alike; `GitHubApp` identifies the
publisher. To answer broad questions ("which commits/PRs failed CI", "how flaky is this area"), checks
are collected for **every ingested commit** — made affordable by **batched GraphQL** (one round-trip per
~10 commits) run in parallel, rather than one REST call per commit. Workflow YAML parsing remains a
separate code/deployment-tier follow-up.

## The three axes (now informed by the above)

- **A. CI** — public Checks API provenance: check suites and runs linked to commits, with publisher
  GitHub Apps represented explicitly.
- **B. History / ownership** — complete reachable `GitCommit`/`GitUser` graph, local Git data,
  append-by-branch-tip; public repository gate applies to the entire layer.
- **C. GitHub platform** — `GitPullRequest`/`GitIssue`/`GitRelease` via the anonymous public GitHub
  API; separate repo-scoped subgraph, refreshed at most daily during ingestion.
  Issue and PR authors connect through `OPENED_BY`; PR text references connect to same-repository
  issues through `REFERENCES_ISSUE`, and merged PRs with explicit closing keywords use `CLOSES_ISSUE`.
  **Opt-in and public-repo-only (no private-repo collection — see Policy).**

## Cross-cutting: PII & the public-repo rule

B and C introduce **human identities**. This feature only collects verified public repositories, and
the user has chosen to retain public contributor identities and public issue/PR content as returned
by GitHub. Private or unverified repositories are excluded before any local history or remote content
is ingested.

## Initial implementation direction (user-approved)

The user chose an all-or-none `git_info.enabled` setting, configured during install. When enabled,
Auryn ingests all locally reachable commits, authors/committers, and tags, plus public GitHub issues,
pull requests, releases, tags, and per-commit check suites/runs. The implementation uses GitPython for
local history, the recycled `gh` token (anonymous fallback) for the REST API, and batched GraphQL both
for check-suites and for resolving each commit author's GitHub login — so a person is a single `GitUser`
(keyed `github:<login>`), not a git-email node plus a login node. It runs **asynchronously** after code
ingestion: a **detached, single-flight `auryn git-info sync` subprocess** that outlives a one-shot
`auryn ingest` (a plain in-process thread would die on interpreter exit), and that a concurrent trigger
for the same graph skips rather than re-collects. It writes repo-scoped provenance labels so code delta
sync never removes the data. CI workflow/run *config* parsing and review-level details remain follow-up.

`auryn git-info enable [path]` sets the setting and syncs that checkout immediately; `auryn git-info sync`
forces one refresh; `auryn git-info watch [path] --interval <s>` runs that forced refresh on a schedule
so future GitHub activity is picked up without a code change. Disabling removes the provenance labels from
existing graphs; a direct config-file change to disabled is reconciled on the next ingest. Changing a
checkout's origin retargets its `GitCheckout` link and prunes the prior repository subgraph when no other
checkout references it.

The collector streams local commit nodes and edges in bounded batches. API pagination follows the
GitHub `Link` header and is restricted to `api.github.com`. Remote API failures do not fail code
ingestion; local Git information is still collected.

## Decided (this ADR)

- **Private repos: never collected.** The entire Git-information layer is gated by an anonymous
  public-visibility check and enabled/disabled through configuration.
- **Provenance is a separate subgraph** (own labels, repo-scoped ids, churn-tolerant joins); code
  delta calculation never owns or deletes it.
- **No webhooks** (poll on-demand/scheduled; webhooks only for a future hosted deployment).

## Follow-up work

1. Parse CI workflow configuration and connect declared build/deploy jobs to the deployment tier.
2. Add review/comment detail with explicit API rate and graph-size limits.
3. Add conditional request support (ETag / `If-Modified-Since` / `since`) so a `git-info watch` poll that
   finds nothing new is nearly free. A naïve page-1 ETag is *unsound* for this subgraph (a `sort=created`
   list head is unchanged when an existing issue/PR merely changes state), so this needs `since`/`updated`
   fetching with incremental merge, not a blanket short-circuit — hence a follow-up rather than shipped
   with the watcher.

## Consequences

- Committing to the separate-subgraph contract now lets any axis land without re-keying, and guarantees
  the code graph stays branch-correct while provenance stays repo-stable.
- Full history has higher storage and initial-ingest cost than an ownership rollup. The feature is
  opt-in, and remote collection is anonymous and public-only.
