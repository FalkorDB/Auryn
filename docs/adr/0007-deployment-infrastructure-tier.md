# ADR 0007 — Deployment & infrastructure tier: an image-keyed contract across IaC ingestors

Status: **Proposed.** Date: 2026-11-18.

Builds on ADR 0004 (cross-repo resolution: external references as upgradable placeholder nodes),
ADR 0006 (the route-keyed HTTP seam), and the [cross-ingestor schema contract](../graph-model.md).

## Context

Dockerfiles, Compose files, Kubernetes manifests, Helm charts and Terraform are **not source
languages** — they describe *artifacts, running services, and cloud resources*. They form a tier
**above** the code graph. Parsing any one of them in isolation is low value; the value is the
**seam** where this tier connects *down* into the code graph (which Project/File a container ships)
and *sideways* into the seams we already have — the route-keyed `APIEndpoint` (ADR 0006) and the
ORM `Table`/`Database`. The payoff is a blast radius that escapes the repo:

```
Terraform aws_ecs_service / k8s Deployment / Compose service
      -[:RUNS_IMAGE]->   Image (registry/app:tag)      ← the universal join node
      <-[:BUILDS_IMAGE]- BuildStage (a Dockerfile)
      -[:PACKAGES]->     code Project / the File(s) it COPYs in
```

Five ingestors (`docker`, `compose`, `k8s`, `helm`, `terraform`) plus CI pipelines will write into
this tier over time. If each invents its own labels/keys they won't compose, exactly the failure the
language [schema contract](../graph-model.md#the-cross-ingestor-contract) was written to prevent.
**This ADR is that contract for the deployment tier**: the shared node labels, the id/join-key
scheme, the edge types, the reconciliation rule, and the producer-vs-stitch split — fixed *once*,
before the first ingestor lands, so a new IaC source is additive.

The organising principle mirrors the two seams we already shipped: a single **hub node** that
producers and consumers key independently and therefore *merge on*. For HTTP the hub is the
route-keyed `APIEndpoint`; for ORM it is the `Table`. **For the deployment tier the hub is the
`Image`** — a Dockerfile *builds* it, a Service/Resource *runs* it, and they converge on one node by
its normalized reference, cross-repo and order-independent.

## Decisions

### 1. Shared node labels

A new IaC ingestor **reuses** these and adds a source-specific label only where a concept genuinely
doesn't map. `Project`/`File`/`APIEndpoint`/`Table`/`Database` are the *existing* code-graph labels
the seams join to — reused verbatim, never shadowed.

| Label | Meaning | Primary producer | Consumers |
|---|---|---|---|
| **`Image`** | A container image, by normalized reference. **The hub node.** | docker (build output) | compose, k8s, helm, tf, ci (run/push input) |
| **`BuildStage`** | One `FROM … [AS name]` stage of a Dockerfile. | docker | compose `build:`, ci |
| **`Service`** | A runnable workload (compose service, k8s Deployment/StatefulSet/DaemonSet/CronJob, ECS service). | compose, k8s | tf, helm, mesh |
| **`Resource`** | An IaC-managed infra object that isn't itself a workload (a cloud resource, a Helm release). | terraform, helm | tf refs |
| **`Registry`** | A container registry / repository (ECR, GHCR, Docker Hub). | tf, ci | docker, k8s |
| `ConfigValue` / `Secret` | A named env/config entry (`Secret` = sensitive; value redacted). | all | the DB/config seam |
| `Port` / `Volume` | *Optional, v2.* Fine-grained wiring; **v1 keeps these as edge properties** (see §6). | all | — |

### 2. Id scheme — and the image reference as the universal join key

Deployment ids follow the code-graph rules ([stable, hierarchical, project-scoped, no inferred
types](../graph-model.md#the-id-scheme)) with one deliberate exception: **the `Image` id is
project-*independent***, exactly like an `extern://` linkage placeholder, because it is the key two
different repos (and two different ingestors) must agree on without coordination.

| Node | Id | Scope |
|---|---|---|
| `Image` | `image://<registry>/<repository>:<tag>` or `image://<registry>/<repository>@<digest>` | **global** (the join key) |
| `BuildStage` | `docker://<project>/<dockerfile-path>#<stage-name-or-index>` | project |
| `Service` (compose) | `compose://<project>/<file>/<service>` | project |
| `Service` (k8s) | `k8s://<project>/<namespace>/<kind>/<name>` | project |
| `Resource` (terraform) | `tf://<project>/<module>/<type>.<name>` | project |
| `Resource`/`Service` (helm) | `helm://<project>/<chart>/<template>/<name>` | project |
| `ConfigValue` / `Secret` | `<owner-id>/env/<NAME>` | owner-scoped |

**Image reference normalization (the whole contract hinges on this — every producer MUST canonicalize
identically).** A Dockerfile `FROM python:3.12-slim`, a Compose `image: python:3.12-slim`, and a k8s
`image: python:3.12-slim` must produce the *same* `Image` id. Rules:

- Default registry `docker.io`; default namespace `library` for a single-component official image
  (`nginx` → `docker.io/library/nginx`); default tag `latest`.
- Lowercase registry + repository (they are case-insensitive); the tag is case-*sensitive*.
- Prefer `@<digest>` over `:<tag>` when a digest is present (it is the stronger identity).
- `${VAR}`/`$(…)` interpolations that can't be statically resolved key a `resolved:false` `Image`
  from the raw text (case-preserved, so the variable name round-trips) with that raw ref kept in a
  property. Note a variable placeholder and the concrete image it later expands to are **different
  `image://` ids** — keeping the raw ref does not auto-merge them; a producer that CAN expand the var
  (Compose/CI, which knows the build args) emits the concrete id, and the raw placeholder is GC'd once
  nothing references it. Within-Dockerfile `ARG` defaults are expanded at parse time; an empty/missing
  `ARG` or a `${VAR:-default}` modifier form is left unresolved rather than half-substituted.
- Normalization lives in **one** shared helper, imported by every producer — never re-implemented per
  ingestor. *(Transitional: the first producer keeps it in `auryn.ingestors.docker.image_ref`; it
  moves to `auryn.core.image_ref` when the second producer lands — §7.)*

### 3. Edge types

| Edge | From → To | Properties / notes |
|---|---|---|
| `HAS_STAGE` | `File` (Dockerfile) → `BuildStage` | structural backbone (`HAS_*` family) |
| `BASED_ON` | `BuildStage`/`Image` → base `Image` | the `FROM` chain; carries `resolution` |
| `COPIES_FROM` | `BuildStage` → `BuildStage` | multi-stage `COPY --from=` |
| **`BUILDS_IMAGE`** | `BuildStage` → `Image` | the published image a build emits (written by the stitch once a tag is assigned) |
| **`RUNS_IMAGE`** | `Service` → `Image` | the image a workload runs |
| `DEPENDS_ON` | `Service` → `Service` | **reuse the contract edge**; compose `depends_on`, k8s/tf refs, Helm chart deps |
| `PROVISIONS` | `Resource` → `Service`/`Image`/`Registry`/`Database` | a Terraform resource creates a runtime thing |
| `DEPLOYS` | `Resource`(release)/ci-job → `Service`/`Resource` | Helm release / CI deploy step |
| `STORED_IN` | `Image` → `Registry` | where an image is pushed/pulled |
| `MOUNTS` | `Service` → `ConfigValue`/`Secret`/`Volume` | config/secret/volume wiring |
| **`PACKAGES`** | `BuildStage` → `Project`/`File` | **the code seam** (§4); from the `BuildStage` pre-publish (no image ref yet), mirrored onto the `Image` once a tag exists |
| **`SERVES`** | `Service` → `APIEndpoint` | **the HTTP seam** (§4); route-keyed, joins the *same* node as ADR 0006 |
| `CONNECTS_TO` | `Service` → `Database`/`Table` | **the ORM seam** (§4), from a connection-string `ConfigValue` |
| **`SHIPS`** | `Service` → `Project`/`File` | **derived collapse edge** (§5) |

### 4. The seams (why the tier exists)

All three are written by the stitch (§7), read-only against the finished graph, joining by a natural
string key — never a symbol — so they are language- and source-agnostic:

- **Code seam — `PACKAGES`.** Resolve a `BuildStage`'s build context to a code `Project` root, and
  each `COPY <src>` path to the `File` nodes under it. Now `impact_of(a_function)` reaches *which
  image ships this file*; combined with `RUNS_IMAGE`/`SHIPS`, *which service/deployment* does.
- **HTTP seam — `SERVES`.** A k8s `Ingress` (or an ALB listener rule) carries host + path; normalized
  by the **same** route rules as ADR 0006, it joins the **same** route-keyed `APIEndpoint` node the
  handler `Method` already `EXPOSES_ENDPOINT`. External URL → handler code, in one graph.
- **ORM seam — `CONNECTS_TO`.** A `ConfigValue`/`Secret` whose value parses as a DB connection string
  (`postgres://…`, a JDBC URL) links its `Service` to the `Database`/`Table` the ORM ingestor
  produced. *Which service talks to which database* becomes a query.

### 5. The derived collapse edge `SHIPS`

The build→run boundary is a "V": `Service -RUNS_IMAGE→ Image ←BUILDS_IMAGE- BuildStage -PACKAGES→
Project`. A single-direction reachability walk (`impact_of` upward, `who_calls`) can't make the
forward-then-backward turn through the hub and **dies at the boundary** — the identical problem ADR
0006 solved for the caller→endpoint←handler V with its derived `INVOKES` edge. The stitch collapses
this V into one `Service -SHIPS→ Project/File` edge (provenance `deploy-stitch`, so it is never
confused with a code edge), so blast radius crosses the build/deploy boundary in a single hop. Like
`INVOKES`, it is a *derived convenience* edge rebuilt from scratch each run; the raw `RUNS_IMAGE`/
`BUILDS_IMAGE`/`PACKAGES` edges remain for provenance and the viewer.

### 6. Reconciliation — image-ref join, mirroring ADR 0004

The `Image` hub is reconciled exactly as symbols are, but keyed on the **normalized reference**
instead of a `linkage_key`:

- A base image a Dockerfile can't build locally (`FROM python:3.12-slim`) is a placeholder
  `Image{resolved:false, origin:"registry"}` — one shared node, every dependent inbound, like the
  single `fmt.Println` node.
- An image **built** in repo A (a `BUILDS_IMAGE` edge from A's `BuildStage`) and **run** in repo B (a
  `RUNS_IMAGE` edge from B's k8s Deployment) key to the *same* `image://…` id, so co-locating the
  repos in one graph resolves the build→deploy link into a real path — **cross-repo, order-independent,
  self-healing on re-ingest**, the ADR 0004 guarantee.
- `resolved` is a pure function of the graph: `true` iff some `BUILDS_IMAGE` points at the image
  (an internally-built image) — recomputed by the stitch, independent of ingest order and of which
  side wrote the node.
- **GC.** An `image://…` node left with **no inbound reference of any kind** — no base (`BASED_ON`),
  no builder (`BUILDS_IMAGE`), no runner (`RUNS_IMAGE`), no provisioner (`PROVISIONS`, a Terraform
  `Resource`) — is the residue of a removed Dockerfile or manifest; the stitch deletes it (predicate +
  delete in one query), scoped to the `image://` prefix so it never touches a project-owned node — the
  same shared-node lifecycle `APIEndpoint` has (ADR 0006). (`COPIES_FROM` is `BuildStage → BuildStage`
  and never targets an `Image`, so it isn't a keep reason.) An external base like `python:3.12-slim`
  keeps its `BASED_ON` referrers, so it is **not** an orphan while any Dockerfile still builds `FROM` it.

**v1 keeps Port/Volume as edge properties**, not nodes (ingestor principle: *invent no node a query
doesn't need*). They are promoted to first-class nodes only when a seam needs to point *at* one.

### 7. Producer vs. stitch split

The same two-role split as `http`/`orm`, so each half stays simple and the join runs once:

- **Producers** (`GitLanguage`, file-suffix/basename scanned, single-phase — IaC is declarative, no
  deep pass) read their own files and emit: the tier's nodes, the *intra-file* edges (`HAS_STAGE`,
  `BASED_ON`, `COPIES_FROM`, `MOUNTS`), and `resolved:false` `Image` placeholders for every image
  reference. They write **no cross-artifact edge** — they don't know what else is in the graph.
- **The stitch** (`GraphEnrichment` named `deploy`, registered in `auryn.enrichments`, opt-out via
  `auryn hooks disable deploy`) runs after the producers on both the full and incremental paths. It is
  **graph-only and idempotent**: it owns and recomputes its layer each run — `BUILDS_IMAGE`,
  `PACKAGES`, `SERVES`, `CONNECTS_TO`, `SHIPS`, the `resolved` flag, and image-node GC — so any ingest
  order converges and a resync self-corrects.

One small, additive core change this tier needs: `GitLanguage` matches by `path.endswith(suffix)`,
which can't express `Dockerfile` / `Dockerfile.prod` / `*.tf` / `values.yaml` basename detection.
Add an optional `filename_matches` predicate to `GitLanguage` (and the two `Source.list_files` call
sites); it is a no-op for the suffix-only languages and lets every IaC producer register a basename
matcher cleanly.

### 8. Provenance

Reuse the existing conventions verbatim, so an agent reads the deployment tier with the same trust
rules as the code graph:

- `resolution` on resolvable edges (`BASED_ON`, `SERVES`, `CONNECTS_TO`): `"declared"` (read straight
  from a manifest) / `"resolved"` (joined by the stitch) / `"external"` (an unresolved placeholder).
- `resolved:false` + `origin` on placeholder `Image` nodes: `origin:"registry"` = a terminal external
  pull (never heals, like a stdlib symbol); `origin:"internal"` = expected to be built by a sibling
  repo (the heal-me signal).
- `provenance` on every stitch-synthesized edge (`deploy-stitch`), so its layer is distinguishable
  and the stitch can clear exactly its own edges on recompute.

## Consequences

- **One hub, five ingestors.** Each IaC source is an additive producer plus a per-source arm of the
  one `deploy` stitch; the `Image` node and its reconciliation are written once. A sixth source
  (serverless, Pulumi, SAM) is the same shape.
- **Blast radius leaves the repo.** `impact_of` / `find_path` / `trace_entrypoint` traverse the new
  edges with **no MCP change** — a code change now reaches the image, service, Helm release and cloud
  resource that ship it, and (via `SERVES`/`CONNECTS_TO`) the external endpoints and databases it
  fronts.
- **Cross-repo build/deploy linkage for free**, by co-locating repos in one graph — the ADR 0004
  property, applied to images.
- **Cost / non-goals (v1).** No runtime/cluster state (this is the *declared* desired state, not what
  is live); no Helm/Kustomize *render* in the default pass (an opt-in `helm template` → k8s pass is a
  follow-up — the static `Chart.yaml`/`values.yaml` graph ships first); Port/Volume stay edge
  properties; secret *values* are redacted, never stored. Supply-chain `DEPENDS_ON_PACKAGE` (lockfile
  → external package) is an adjacent, separately-scoped ingestor family, not this tier.
- `graph-model.md` gains a **"Deployment & infrastructure tier"** section (per-source map) when the
  first producer (`auryn-docker` + the `deploy` stitch) lands; this ADR is the contract it will
  document.

## Per-source contribution map (the checklist each ingestor implements)

| Ingestor | Emits (nodes) | Emits (intra-file edges) | Stitch arm adds |
|---|---|---|---|
| **docker** | `BuildStage`, placeholder `Image` (bases) | `HAS_STAGE`, `BASED_ON`, `COPIES_FROM` | `PACKAGES` (context+COPY → Project/File). *Not `BUILDS_IMAGE`*: a Dockerfile doesn't name the image it produces — the tag comes from Compose `build:`/`image:` or CI `-t`, so that edge is the Compose/CI arm's (keyed by the normalized ref, matched to the `BuildStage` by build context). |
| **compose** | `Service`, `Image` (from `image:`) | `DEPENDS_ON`, `MOUNTS`, `RUNS_IMAGE` | resolve `build:` → the Dockerfile `BuildStage` (`BUILDS_IMAGE`); DB service → `CONNECTS_TO` |
| **k8s** | `Service`, `ConfigValue`/`Secret`, `Image` | `RUNS_IMAGE`, `MOUNTS`, label-selector `DEPENDS_ON` | Ingress route → `SERVES` APIEndpoint; connection string → `CONNECTS_TO` |
| **helm** | `Resource`(release), `Service`, `ConfigValue` | `DEPENDS_ON` (chart deps), `RUNS_IMAGE` (values image) | optional `helm template` → k8s arm |
| **terraform** | `Resource`, `Registry`, `Image` | `DEPENDS_ON`/`PROVISIONS` (HCL refs) | task-def image → `BUILDS_IMAGE`; `rds` → `CONNECTS_TO`; `lambda` src → `PACKAGES`; `lb`/`api_gw` → `SERVES` |
| **ci** (follow-up) | ci-job `Resource`, `Image`, `Registry` | build/push steps | `BUILDS_IMAGE` + `STORED_IN`; deploy step → `DEPLOYS` |
