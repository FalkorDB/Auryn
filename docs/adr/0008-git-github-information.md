# ADR 0008 — Git & GitHub information: the repository-provenance layer

Status: **Proposed (design only — no code yet).** Date: 2026-11-19.

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
| **A. CI** | what builds & deploys this image/service? | workflow YAML (**in-repo**) + optionally the runs API | static parse (config) / API (runs) | no (config) / yes (runs) |
| **B. History / ownership** | who wrote / last touched / how hot is this? | `git log` (local) | append-by-commit | no |
| **C. GitHub platform** | which PR/issue/review changed this; who owns this path? | GitHub/GitLab API | networked, cursor-paged | yes |

The decisions below are driven by five concerns the reviewer raised: **(1)** how we connect to the
remote to pull PRs/issues, **(2)** keeping this *out* of the branch-/commit-switched code sync,
**(3)** which fields to index, **(4)** webhooks vs polling, **(5)** whether to track CI — all under a
**memory-vs-value** lens and "how the user sets it up".

## The architectural decision: provenance is a SEPARATE subgraph with its own lifecycle

This is the spine that answers concern **(2)** and shapes the rest.

- **Separate labels.** Provenance nodes (`Commit`, `Contributor`, `PullRequest`, `Issue`, `Review`,
  `CIRun`, …) use labels that are **not in any code engine's `OWNED_LABELS`**. The code's scoped
  incremental resync (`stream_apply(proj_id, <code labels>, …)`, ADR 0002/0005) is label-scoped, so a
  **branch switch / commit / save-watcher re-ingest never touches provenance** — and vice-versa.
- **Separate trigger & cadence.** Code triggers (commit hook, save watcher, branch checkout) must
  **not** fire a remote pull — it's expensive, rate-limited, and repo-level-invariant. Provenance syncs
  on its own cadence: **on-demand** (`auryn github-sync` / an MCP tool) and an **optional scheduled
  refresh**. Switching branches re-syncs only code.
- **Repo-scoped keys, not commit-scoped.** PRs/issues/commits key on the **repo + their own id**
  (`github://<owner>/<repo>/pull/<n>`, `git://<repo>/<sha>`), so they persist across checkouts.
- **Joins tolerate code churn.** Provenance→code edges reference code by **stable key** (file path,
  symbol `linkage_key`/qname, commit SHA). When the current branch lacks a referenced `File`/`Function`,
  the edge simply has no in-branch target (or targets an ADR-0004-style placeholder that *heals* when
  the code node exists). Neither sync deletes the other's nodes.
- **One exception — CI *config* is code.** Workflow YAML lives in the checkout, so it is **branch-tied
  source**: parsed by the code/deployment-tier sync, not the provenance sync. Only CI *runs* are
  provenance. (See §A.)

## Policy: public repos only, opt-in, via the GitHub CLI

To avoid data leakage, **Auryn never pulls from a private GitHub/GitLab repo.** The networked github
data — axis **C** (Issues/PRs/reviews) and **CI runs** — is collected **only when the user explicitly
asks** (opt-in: an `auryn github-sync` command / MCP tool / config flag) and **only for public repos**:
- **Via the user's GitHub CLI** (`gh`; `glab` for GitLab) — Auryn shells out to it, so the CLI owns
  authentication and **Auryn stores, sees, and logs no token** of its own.
- **Public-repo gate:** before any content pull, check visibility (`gh repo view <o>/<r> --json
  isPrivate`, or the GitLab equivalent) and **refuse a private repo** (log why). Public data carries no
  leakage risk.
- **Never automatic:** not triggered by `ingest`, a commit hook, the save-watcher, or a branch switch
  (the separate-trigger rule) — the user initiates it.
- The **recommended v1 is unaffected** — it is **entirely local** (CI config + ownership rollup from the
  checkout + `git log`) and pulls from no repo at all.

## Automatic? No — network egress is opt-in and explicit

**Auryn never auto-pulls from a (private or public) GitHub repo as a side-effect of ingesting code.**
- The **recommended v1 (CI config + ownership rollup) makes zero network calls** — it reads only the
  checkout's workflow YAML and local `git log`. Ingesting a private repo phones nothing.
- The only networked axes — **C (PRs/issues/reviews) and CI *runs*** — are **opt-in and explicit**: a
  deliberate `auryn github-sync` command / an MCP tool / a config flag the user turns on. They are
  **never triggered by `ingest`, a commit hook, the save-watcher, or a branch switch** (the
  separate-trigger rule above). The **first** remote pull requires an explicit opt-in that states it
  reads via the user's `gh`/token access; a scheduled refresh is itself opt-in.
- Private data that lands in the graph is governed by the PII / no-client-data rule below (redact/hash
  identities; never in a shared/public/demo graph).

## (1) Connecting to the remote (for C, and CI runs) — via the CLI

- **Identify the repo** from the checkout's `origin` remote (git_sync already carries `repo_uri`) →
  `(host, owner, repo)`; pick `gh` (GitHub) vs `glab` (GitLab) from the host.
- **Auth is delegated to the CLI.** Auryn shells out to `gh`/`glab` (e.g. `gh api
  repos/{owner}/{repo}/issues --paginate`), so the CLI's existing login is used and **Auryn never sees,
  stores, or logs a token.** If the CLI is absent / not logged in, the github-data step is **skipped
  with a clear log** — it never blocks the code ingest.
- **Public-only gate:** `gh repo view <o>/<r> --json isPrivate` (or the GitLab equivalent) must report
  **public** before any content is pulled; a private repo is **refused** (policy above).

## (2′) Keeping provenance out of the code sync — summary

Covered by the separate-subgraph spine above: distinct labels (not resync-scoped), distinct triggers
(no remote pull on branch switch), repo-scoped ids, churn-tolerant joins. Net effect the reviewer asked
for: **switching branches/commits re-ingests code only; PR/issue/history/CI-run data is untouched.**

## (3) Which fields to index — and memory

Follow the schema.py rule: **index only join/lookup keys and `WHERE`-clause fields; store the rest as
unindexed properties.** High-cardinality free text (PR/issue titles & bodies, commit messages) is
**not** indexed unless a feature needs full-text over it — and a full-text index is a real memory cost
(ADR 0007/embeddings), so it's opt-in.

| Node | Indexed | Stored (unindexed) |
|---|---|---|
| `Commit` | `sha` | message, date, insertions/deletions |
| `Contributor` | `identity` (normalized, **hashed by default** — see PII) | display name (opt-in) |
| `PullRequest`/`Issue` | `number`, `merge_commit_sha`/`head_sha` (join to `Commit`) | title, state, labels |
| `CIRun` | `commit_sha`, image ref via the shared `image://` key | status, started_at |

**Memory is the dominant design lever for history (B).** A busy repo has 10⁵+ commits; a full
`Commit` + `CHANGED`-edge graph can **dwarf the code graph**. So B defaults to an **ownership ROLLUP**,
not a history graph: aggregate `git log` into **properties on the existing `File`/`Function` nodes** —
`last_author`, `last_changed`, `commit_count`/`churn`, `author_count`. That delivers ~all the *value*
(ownership, recency, hotspots, reviewer suggestion) at a **tiny fraction of the memory** (no node/edge
explosion, nothing new to index — the props are read on an already-located node). The full
`Commit`/`Contributor` graph is **opt-in and bounded** (last-N / since-date) for users who want
commit-level traversal. History is immutable → the rollup/append sync is cheap and incremental-by-SHA.

## (4) Webhooks vs polling

**Polling, not webhooks, for v1.** Webhooks need a **public HTTPS endpoint** to receive GitHub's POSTs
plus per-repo webhook setup and secret handling — infrastructure a **local-first** tool doesn't have
and shouldn't require a user to stand up. Instead: **on-demand + optional scheduled pull**, using
**conditional requests (ETag / `If-Modified-Since`) and `since`/cursor pagination** so a refresh is
cheap and rate-limit-friendly. Webhooks remain a reasonable *later* option for a **hosted/server**
deployment (push-based, real-time) — not the default.

## (5) Do we track CI? — split config vs runs

**Yes to CI config; defer CI runs.**

- **CI config (static, in-repo) — build now, highest value/cost ratio.** Parsing `.github/workflows/*`
  / `.gitlab-ci.yml` needs **no auth**, is **branch-tied source** (synced with code/deployment tier,
  not provenance), and **completes the ADR 0007 loop**: a `CIJob` `BUILDS_IMAGE` → the shared `Image`
  hub, `STORED_IN` → `Registry`, `DEPLOYS` → the k8s/compose/Helm `Service`. (ADR 0007 already reserved
  this `ci` row.) Recognize `docker build/push` and `kubectl/helm` steps with a small command-pattern
  matcher (not a shell interpreter). Low memory, low risk.
- **CI runs (dynamic, API) — provenance, defer.** Run history / pass-fail / which-commit-deployed is
  API-based (auth, rate limits) and more *operational* than *structural* — lower value for a code graph
  and unbounded memory (run history grows forever). Opt-in later if demanded; keep only a bounded recent
  window.

## The three axes (now informed by the above)

- **A. CI** — config as a code/deployment-tier producer (`auryn-ci`, extends ADR 0007); runs deferred.
  Nodes `Pipeline`/`CIJob` (a classified `Resource`); edges `HAS_JOB`/`BUILDS_IMAGE`/`STORED_IN`/
  `DEPLOYS`/`NEEDS`; the `deploy-ci` arm runs the shared `core.deploy` image lifecycle.
- **B. History / ownership** — **rollup-by-default** (props on `File`/`Function`), full
  `Commit`/`Contributor` graph opt-in + bounded; local `git log`, append-by-SHA, no auth; **PII-gated**.
- **C. GitHub platform** — `PullRequest`/`Issue`/`Review`/`Owner` (CODEOWNERS) via the **`gh`/`glab`
  CLI**; separate subgraph, poll-based, joins to `Commit`/`File`/`Package` by stable key; **PII-gated**.
  **Opt-in and public-repo-only (no private-repo pulls — see Policy).**

## Cross-cutting: PII & the public-repo rule

B and C introduce **human identities**. The hard rule is **no client/prospect data in the public repo**.
So identities are **redacted/hashed by default**, real identities are **opt-in**, and **never** in a
public/demo graph. This is the gating decision for B and C.

## Recommended v1 (best value per memory, least setup — and zero network)

Both are **entirely local — no GitHub API, no egress, no token, nothing pulled from a private repo:**

1. **CI config** (A) — extends ADR 0007, no auth, branch-tied, completes the build→deploy loop.
2. **Ownership rollup** (B-lite) — props on `File`/`Function` from `git log`, bounded, redacted; big
   value (ownership/recency/hotspots) at near-zero memory.

Everything else — full commit graph, PR/issue/review graph (C), CI runs — is **opt-in later**, each
ratified in its own ADR (C especially: auth + rate-limit + poll model + PII).

## Decided (this ADR)

- **Private repos: never pulled.** Networked github data is **public-repo-only**, **opt-in** (explicit,
  never automatic), and fetched **via the user's `gh`/`glab` CLI** (no token handled by Auryn).
- **Provenance is a separate subgraph** (own labels, own trigger/cadence, repo-scoped ids,
  churn-tolerant joins) → a branch/commit switch never touches it, and it never fires on a code ingest.
- **No webhooks** (poll on-demand/scheduled; webhooks only for a future hosted deployment).

## Still open for the reviewer

1. **History granularity:** ownership **ROLLUP** (props on File/Function) by default, with a full
   `Commit`/`Contributor` graph opt-in + bounded — or commit-level nodes from the start (memory cost)?
2. **PII of public data:** author names in public PRs/issues/commits are low-stakes, but still —
   redact/hash by default, or keep real identities for public repos?
3. **Sequencing / appetite:** is the near-term build the **public github-data collector** (C + CI runs
   via `gh`, opt-in) — the "real value" you flagged — the **local v1** (CI config + ownership rollup),
   or both?

## Consequences

- Committing to the separate-subgraph contract now lets any axis land without re-keying, and guarantees
  the code graph stays branch-correct while provenance stays repo-stable.
- The rollup-first choice for history makes the highest-value ownership signal essentially free on
  memory; the full history/PR/issue/run graphs are deliberately opt-in behind their memory/auth costs.
