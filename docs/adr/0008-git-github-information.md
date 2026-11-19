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

## (1) Connecting to the remote (for C, and CI runs)

- **Identify the repo** from the checkout's `origin` remote (git_sync already carries `repo_uri`) →
  `(host, owner, repo)`; pick the GitHub vs GitLab client from the host. No manual config in the common
  case.
- **Auth, in precedence order** (zero-config first): the **`gh`/`glab` CLI token** if installed
  (`gh auth token`) → an **`AURYN_GITHUB_TOKEN`/`AURYN_GITLAB_TOKEN`** env var or config entry →
  unauthenticated (public repos only, 60 req/hr — enough to *try*, with a clear "set a token for
  private repos / higher limits" log). A GitHub App is a later option for orgs.
- **Graceful degradation:** a non-GitHub/GitLab remote, or no token for a private repo, **skips axis C
  and logs why** — it never blocks the code ingest. The token is read from the existing connection/config
  path and **never written into the graph or logs**.

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
- **C. GitHub platform** — `PullRequest`/`Issue`/`Review`/`Owner` (CODEOWNERS) via the API; separate
  subgraph, poll-based, auth per (1); joins to `Commit`/`File`/`Package` by stable key; **PII-gated**.

## Cross-cutting: PII & the public-repo rule

B and C introduce **human identities**. The hard rule is **no client/prospect data in the public repo**.
So identities are **redacted/hashed by default**, real identities are **opt-in**, and **never** in a
public/demo graph. This is the gating decision for B and C.

## Recommended v1 (best value per memory, least setup)

1. **CI config** (A) — extends ADR 0007, no auth, branch-tied, completes the build→deploy loop.
2. **Ownership rollup** (B-lite) — props on `File`/`Function` from `git log`, bounded, redacted; big
   value (ownership/recency/hotspots) at near-zero memory.

Everything else — full commit graph, PR/issue/review graph (C), CI runs — is **opt-in later**, each
ratified in its own ADR (C especially: auth + rate-limit + poll model + PII).

## Open questions for the reviewer

1. **Agree with the separate-subgraph + separate-trigger model** (provenance never touched by a branch
   switch; synced on-demand/scheduled, not by the commit hook)?
2. **Ownership as a ROLLUP (props on File/Function) by default**, full commit graph opt-in — or do you
   want commit-level nodes from the start (memory cost)?
3. **PII posture:** redact/hash identities by default, opt-in to real (recommended)?
4. **Polling (no webhooks) for v1** — agreed, with webhooks reserved for a hosted deployment?
5. **CI = config now, runs deferred** — agreed? (Config is the ADR-0007 loop-closer.)
6. **Connection/auth:** derive repo from the remote; `gh`/`glab` token → env/config → unauth-public,
   with graceful skip — acceptable default?

## Consequences

- Committing to the separate-subgraph contract now lets any axis land without re-keying, and guarantees
  the code graph stays branch-correct while provenance stays repo-stable.
- The rollup-first choice for history makes the highest-value ownership signal essentially free on
  memory; the full history/PR/issue/run graphs are deliberately opt-in behind their memory/auth costs.
