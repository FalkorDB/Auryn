# ADR 0008 — Git & GitHub information: the repository-provenance layer

Status: **Proposed (design only — no code yet).** Date: 2026-11-19.

Builds on ADR 0007 (the image-keyed deployment tier), ADR 0004 (external references as upgradable
placeholder nodes), and the [cross-ingestor schema contract](../graph-model.md).

## Context

The graph models two things well today: **code structure** (modules, classes, functions, calls,
imports — per language) and, since ADR 0007, the **deployment tier** (code → image → service across
Docker/Compose/k8s/Terraform/Helm). What it does *not* model is the **provenance** around that code —
how the artifacts get built and shipped, who wrote and owns the code, and the process that changed it.
That is "git & GitHub information", and it is three genuinely different things with three different
data sources and ingestion modes. This ADR scopes them, picks join keys to the existing graph, and
recommends a sequencing. It deliberately decides *nothing to build yet* — it exists so we commit to a
contract before the first producer, as ADR 0007 did for the deployment tier.

The three axes:

| Axis | Answers | Data source | Ingestion mode |
|---|---|---|---|
| **A. CI pipelines** | "what builds & deploys this image / service?" | workflow YAML in the repo | static file parse (like every ingestor) |
| **B. Git history / ownership** | "who wrote / last touched / how volatile is this?" | `git log` | temporal, append-by-commit-range |
| **C. GitHub platform** | "which PR/issue/review changed this; who owns this path?" | GitHub REST/GraphQL API | networked, auth'd, rate-limited |

## Decision (sequencing)

Build **A first**, then **B**, then **C** — increasing distance from the existing static-parse model
and increasing infrastructure cost. A extends ADR 0007 directly; B and C each warrant their own ADR
when we take them on (this one records the intent and the contract sketch).

---

## A. CI pipelines — the build/deploy producer (extends ADR 0007)

ADR 0007 already **reserves** this row (`ci (follow-up) | ci-job Resource, Image, Registry | …`). The
deployment tier models `code → image → service` but not *who builds and ships the image* — CI is that
missing glue, and it closes the loop: a `BUILDS_IMAGE` from a CI job to the same shared `Image` hub a
Dockerfile stage targets, and a `DEPLOYS` to the k8s/compose/Helm `Service` that runs it.

**Producer:** a new `auryn-ci` GitLanguage (static parse, no render), detecting:
- GitHub Actions — `.github/workflows/*.yml`,
- GitLab CI — `.gitlab-ci.yml`,
- (later) CircleCI / Jenkins / Azure Pipelines.

**Nodes** (join the ADR 0007 contract; the `Image` hub is the one global key):

| Label | What | Id |
|---|---|---|
| `Pipeline` | one workflow file | `ci://<project>/<path>` |
| `CIJob` (a `Resource`, classified `ci-job`) | a job/step group | `ci://<project>/<path>/<job>` |
| `Image` | shared hub, via `core.image_ref` | `image://<normalized-ref>` |
| `Registry` | shared, via `core.image_ref` registry key (ADR 0007 §note: producer-scoped in v1) | per ADR 0007 |

**Edges** (reuse the ADR 0007 relationships):

| Edge | From → To | Recognized from |
|---|---|---|
| `HAS_JOB` | `Pipeline` → `CIJob` | workflow structure |
| `BUILDS_IMAGE` | `CIJob` → `Image` | a `docker build -t X` / `docker/build-push-action` step (the tag the Dockerfile stage didn't name — ADR 0007 docker row) |
| `STORED_IN` | `Image` → `Registry` | a `docker push` / registry-login step |
| `DEPLOYS` | `CIJob` → `Service`/`Resource` | a `kubectl apply` / `helm upgrade` / compose-up step (matched to the workload by image or name) |
| `NEEDS` | `CIJob` → `CIJob` | `needs:` / stage ordering |

**Stitch:** a `deploy-ci` enrichment runs the shared `core.deploy` image lifecycle (like every other
tier arm), so a CI-built image flips `resolved/internal` and reaches a function's blast radius.

**v1 scope:** GitHub Actions + GitLab CI; recognize `docker build/push` and `kubectl/helm` deploy
steps by a small command-pattern matcher (not a shell interpreter). Everything else is a `CIJob` with
no tier edge — safe. **Effort: low-med; risk: low** (same pattern as the 5 shipped producers).

## B. Git history / ownership — a new temporal axis (own ADR when taken)

Ingest `git log` into the graph: **who** changed **what**, **when**, and **how often**.

**Nodes:** `Commit` (`git://<project>/<sha>`, with message/date), `Contributor`
(`git://<project>/author/<normalized-identity>`).

**Edges:** `AUTHORED` (`Contributor` → `Commit`), `CHANGED` (`Commit` → `File`), and — if we go
function-granular — `CHANGED` (`Commit` → `Function`/`Method` via the diff's line ranges mapped through
`impact.symbols_in_ranges`, the same machinery `impact_of_diff` uses). Derived properties: `last_changed`,
`change_count`/`churn`, `author_count` on File/Function nodes.

**Why it's powerful:** combined with the code graph it answers "who owns / last touched X", "what's the
riskiest (highest-churn) code in this blast radius", "who should review a change to Y" — an ownership/
recency axis the structural graph can't express.

**Open design decisions (for its ADR):**
- **Granularity:** file-level (cheap, robust) vs function-level (needs per-commit line-range → symbol
  mapping; much richer, more compute, and line ranges drift as code moves).
- **Incremental model:** append new commits since the stored HEAD (history is immutable → pure append,
  no diff-reconcile), but handle force-push/rebase (SHAs change).
- **PII:** author names/emails are personal data. The project's hard rule is **no client/prospect data
  in the public repo**; author identities in a shared graph need an explicit decision — redact/hash by
  default, opt-in to real identities, and never for a public-repo demo graph. **This is the gating call.**

**Effort: medium; risk: medium** (new ingestion mode + the PII decision).

## C. GitHub platform — code↔process (own ADR; API-based)

PRs, issues, reviews, and `CODEOWNERS` via the GitHub API — tying code to the process around it.

**Nodes:** `PullRequest`, `Issue`, `Review`, `Owner` (a team/user from CODEOWNERS).
**Edges:** PR `MERGED_AS` `Commit`; PR/Issue `TOUCHES`/`REFERENCES` `File`; `Review` `BY` `Contributor`;
`Owner` `OWNS` a path glob → `File`/`Package`.

**Why last:** it's the only axis that needs **network + auth** (a GitHub token), is **rate-limited**,
and isn't reproducible from a plain checkout — a different operational model from every current
producer. Its incremental sync is API-cursor-based, not git-based.

**Effort: high; risk: med-high** (auth, rate limits, API pagination, token scope/secrets handling).

## Cross-cutting

- **Join keys:** all three join the existing graph on nodes we already emit — `File`/`Module`/
  `Function`/`Method` (code), and for A the shared `Image`/`Service`/`Resource` (ADR 0007). No new
  global key is introduced (A reuses the `image://` hub; B/C key on `File` path + symbol line ranges).
- **PII & the public-repo rule:** B and C introduce human identities. Default to redacted/hashed
  identities; real identities are opt-in and never in a public/demo graph. (See the public-repo
  no-client-data rule.)
- **Auth & secrets:** only C needs a token; it must use the same connection/config path as the rest,
  never embed a token in the graph or logs.
- **Incremental:** A is a normal file-delta (ADR 0002/0005). B is append-by-SHA (immutable history).
  C is API-cursor — each needs its own resync story, defined in its ADR.

## Open questions for the reviewer

1. **Scope of "git/GitHub information" you actually want** — is the near-term goal the CI producer
   (completing the ADR 0007 tier), the ownership/history axis, or the PR/issue process layer?
2. **Git-history granularity** — file-level to start, or commit to the function-level line-range mapping?
3. **PII posture** — redact/hash identities by default (recommended), or ingest real identities with an
   opt-in?
4. **Is CI worth a full `auryn-ci` producer now**, or should it stay a reserved ADR-0007 row until a
   demo repo with a real container-building workflow exists to validate it against?

## Consequences

- Picking the contract now (as ADR 0007 did) lets any of the three land without re-keying later.
- A is a natural, low-risk extension that finishes the deployment-tier story; B and C are larger, newer
  axes gated on the PII decision (B) and the auth/rate-limit model (C) — each to be ratified in its own
  ADR before code.
