# ADR 0005 — Delta-sync for all languages (provenance + refcounted delete)

Status: **Implemented for C and validated on redis/src (real-repo git oracle EQUAL); not yet committed.**
Three fixes carried it from "green on synthetic oracles but wrong on redis" to correct-and-fast:
(1) **definition-vs-declaration ownership** — `DEFINES` owns a symbol's node/CALLS/children, `DECLARES`
only its edge (a shared header no longer strips the CALLS of the hundreds of unrelated symbols it
declares; fixed 5,706 wrongly-removed CALLS); (2) **multi-program name collisions** — sort files by path
for an order-independent multi-definer winner + a transitive co-definer closure + deterministic macro
dedup (redis has 8 `main`, 2 `struct config`); (3) **v2 blast-radius narrowing** — reverse-dep closure
targets only ADDED/REMOVED symbols, so editing a body / adding a symbol pulls no callers. Result:
`full(TARGET) == full(BASE)+incremental(TARGET)` on redis (git path), 84 delta/sync tests green across all
languages, and the delta engine runs **~30×** faster than a full recompute (read-back 0.94s→0.007s, parse
1 file not 185). End-to-end git-commit path is ~3.5× for small edits, capped by ~1.3s of fixed
git-clone/diff/detection overhead shared by full and incremental (a separate optimization).

**Carry-over validated on a second language (Go).** Go incremental was added touching ONLY `packages/go/`
+ the test corpus — `packages/core/` was not modified — confirming the engine, closures, refcount/demote,
and v2 narrowing are generic and driven entirely by `ScopedSpec`. `incremental == full` is EQUAL on a real
multi-package repo (gin, 59 files / 9 packages) across leaf-add and delete+rename+add-exported changes, and
green on the synthetic oracle (incl. the two-`package main` collision). Per-language work matched the ~20%
contract below: add `File-[:DEFINES]→entity` for all entities, write `read_index`+`resolve_incremental`,
supply `defined_symbol_ids`. One contract addition surfaced: the order-independent-winner invariant
(sort-by-path + keep-first node-dedup) must be enforced in each language's *emitter* — C got it free from its
`nodes` dict; Go yields per-symbol and needed an explicit keep-first dedup. So the contract requires: **a
language's `resolve`/`resolve_incremental` must emit each node id at most once, deterministically.**

**Carry-over validated on Java and Rust — and the aggregation tier is now correctly GC'd (a real bug the
first Go/Java passes hid).** Java and Rust were added the same way (per-language glue only). Rust's real-repo
oracle (regex-automata) exposed that the **aggregation tier** (Module / Package) was never provenance-owned,
so a module/package whose last file is renamed or deleted **stranded a ghost node** a full recompute doesn't
have — a bug Go and Java shared (uncaught only because their corpora never deleted a package's last file). The
generic fix, driven by a new `ScopedSpec.aggregation_labels`:
1. **Provenance for the aggregation tier.** Each declaring file emits `File-[:DEFINES]->Module|Package` (Java:
   one edge per package *prefix*), so the node is reachable for GC like any other. Its props must be **fully
   id-derived** (Rust's `Module` dropped `is_declaration` for this) so a multiply-authored one reduces to the
   identical node regardless of author.
2. **Refcount delete, not pull-and-diff.** An aggregation node is co-authored by *every* file in the
   package/module, so it is excluded from the read-back's owned set, the reverse-dep/co-contributor closures,
   and the co-DEFINER winner closure. After the write it is reclaimed **iff it has zero remaining
   `File-[:DEFINES]->` edges** (`_reclaim_orphaned_aggregation`) — correct for a leaf *and* an intermediate
   prefix, and O(change) not O(project): deleting one file in a huge shared package (Java's `com`) no longer
   drags the whole repo in. Without this, the crate-root collapse that conflates every Rust file's inline
   `mod tests {}` into one node made a one-line edit reparse ~⅔ of the crate.

`incremental == full` is EQUAL on real repos across the aggregation lifecycle: **regex-automata** (module add /
rename / delete), **gin** (package add / delete, incr parsed 0–1 files), **gson** (package add / delete). Delta
benchmarks: C ~18.7×, Go ~11.6×, Java ~10.6×, **Rust ~10.3×** (2.57s→0.25s, 3/71 files). Two core changes ride
along, both no-ops for every non-aggregation language (`aggregation_labels` defaults to `()`): the
`aggregation_labels` machinery above, and `demote_node` gaining a `child_sep` arg so a crate-root caller
(`rust://p.fn.x`, whose separator sorts below `/`) is correctly classed intra-project on a cross-repo delete.

Historical (initial engine as built, before the three fixes above): the authored-shared engine is implemented and wired
(`core/incremental_scoped.incremental_sync_authored`, dispatched by `route_incremental` on
`ScopedSpec.provenance_rels`; C binding in `ingestors/c/incremental.py`), and green against the
differential oracle across all three scenarios: standalone (`tests/sync/test_delta_differential.py`
`[c]`), multi-repo, and cross-repo-**bidirectional** (`tests/sync/test_delta_cross_repo.py` — export
delete→demote, re-add→upgrade, import add/drop, and a file-rename of a cross-repo definer). Tier-0
(Python/TS/Bash) is byte-for-byte unchanged and still green. Measured on redis/src (185 files):
full recompute ~1.87s/edit → incremental ~0.05s, **~36–41×** (`benchmarks/freshness/measure_c_incremental.py`).
Tier-1 (Java/Go/Rust/Kotlin/PHP) and the rest of Tier-2 remain future work.
Supersedes the Tier-2/Tier-3 sketch in [ADR 0002](0002-incremental-resync.md) with a concrete,
uniform design validated against an audit of all 15 git-driven ingestors.
Date: 2026-09-21.

## Context

ADR 0002 made resync O(change + blast-radius) for *file-scoped-id* languages and shipped it
for Python (`ast`), TypeScript, and Bash — the only three ingestors on `route_incremental`
today. Every other language (Java, Go, Rust, Kotlin, PHP, C, C++, C#, Ruby, Swift, Julia,
file-based PL/SQL) still does a full O(repo) recompute on every commit: re-parse every file,
resolve globally, diff, write. On large repos that is the dominant cost, and it is the cost
we most want gone.

ADR 0002 already diagnosed *why* the other languages were excluded and sketched the fix
("a per-file ownership handle … `File-[:DEFINES|DECLARES]->x`, delete-by-refcount rather
than DETACH-by-prefix"), but left it as unscoped future work. This ADR turns that sketch into
a concrete, uniform decision, informed by a full audit of every ingestor's id scheme,
shared-node model, resolution model, and existing provenance (see the
`delta-sync-generalization-audit` memory for the raw classification).

The audit's central finding reframes the problem: **the hard boundary is not multi-file
entities — it is deep whole-program inference.** Every multi-file / shared-node case that
looked scary (a C function split across `.h`/`.c`, a namespace spanning files, a PL/SQL
package whose spec and body live apart) is solvable with additive per-file provenance and a
refcounted delete. The one place a bounded delta is *provably impossible* without new
information is a deep-inference engine (Jedi, Roslyn, and future JDT/gopls/tsserver/libclang),
where an inferred edge in an untouched file can change because a distant file changed, with
no syntactic signal and no in-graph predictor. And that is the **default** engine for our two
biggest languages (Python→Jedi, C#→Roslyn).

## The correctness condition

Delta-sync is provably equivalent to a from-scratch ingest at the target commit — the
`apply_delta(full(A), diff A→B) ≡ full(B)` oracle — for a language **iff**:

- **C1 — Provenance.** Every node/edge's *owning-file set* is recoverable from the graph
  alone. (Today: file-scoped-id languages get this for free; FQN/linkage-keyed languages do
  not, except C's `DECLARES`/`DEFINES` and C#'s type-level `DEFINED_IN`.)
- **C2 — Reducibility.** Each element's value is either owned by exactly one file, or a
  deterministic reduction over its owning-file set that can be recomputed by re-reading
  *exactly that set* (which the closure pulls in).
- **C3 — Resolution locality.** Cross-file edges are either **syntactic** (recomputable from
  the graph reverse-dependency closure) **or**, if inferred, the inference's *dependency*
  provenance is captured so affected edges in untouched files are findable.

The delete-or-demote rule and the reverse closure from ADR 0002 are correct whenever C1–C3
hold. The whole roster then reduces to: make C1 true everywhere (additive provenance), satisfy
C2 with a bounded co-contributor closure, and honour C3 by keeping deep engines on full
recompute until inference-dependency provenance exists.

## Two axes, four tiers

Every entity is classified on **A = key granularity** (file-scoped vs coarse, and coarse
splits into *aggregation-point* — a container that only holds per-file entities — vs
*authored-shared* — one node whose own content comes from ≥2 files) and **B = resolution**
(syntactic vs deep-inference).

- **Tier 0 — file-scoped + syntactic. Works today.** TypeScript, Bash, Python-`ast`. On the
  shared engine, green against the oracle. (Python's sole coarse node, `Package`, is an
  aggregation point handled without a refcount via the child-owned-containment rule.)
- **Tier 1 — aggregation-coarse + syntactic.** Java, Go, Rust, Kotlin, PHP. The container
  (package/namespace/module) is a pure aggregation point: no file authors its content, so it
  needs **no node refcount** — only the existing child-owned-containment rule plus an
  orphan-GC that drops the container when its last child edge goes. The real gap is C1: entity
  ids are FQN-keyed and there is **no per-file provenance for non-type members**, so the
  engine cannot enumerate "this file's functions" to retract them.
- **Tier 2 — authored-shared + syntactic.** C, C#-`treesitter`, Ruby, Swift, Julia, PL/SQL,
  C++. Needs the full machine: per-file provenance at member/edge granularity, a refcounted
  delete-or-demote, and C2 property-reduction over the (small) co-contributor closure.
  Readiness varies sharply: **C is closest** (it already emits `File-[:DECLARES/DEFINES]→node`
  — this is the classic `.h`/`.c` case and it is the *most* ready, not the hardest); **PL/SQL
  and C++ need the most** (PL/SQL has no `File` node at all; C++ records no provenance for
  functions, methods, or namespaces).
- **Tier 3 — deep-inference (any A). Boundary.** Python-Jedi (default), C#-Roslyn (default),
  future JDT/gopls/tsserver/libclang. **No provably-correct bounded delta** — C3 fails. These
  stay full-recompute.

## Decision

### 1. Uniform per-file provenance (schema — additive, non-breaking)

Add one canonical provenance edge, emitted by **every** ingestor for **every** entity node:

- `File-[:DEFINES]→entity` — the file authored this node's definition.
- `File-[:DECLARES]→entity` — the file contributes a declaration only (C headers, PL/SQL
  package spec, C++ in-class method declaration whose body is out-of-line).

An authored-shared node therefore carries **multiple** `DEFINES`/`DECLARES` edges, one per
contributing file — the handle that makes refcounted delete expressible. This is the direction
ADR 0002 named.

**One direction, everywhere — no per-language variants.** The provenance edge is
`File-[:DEFINES]→entity` (and `File-[:DECLARES]→entity`) for *every* language. C already emits
exactly this and is the reference. C#'s existing reverse-direction, type-only
`Type-[:DEFINED_IN]→File` is **migrated** to `File-[:DEFINES]→entity` at member granularity —
the repo is pre-release, so a rename is cheaper than carrying a direction switch through the
engine forever. The `ScopedSpec` names the two rels but not a direction; the engine assumes
`File`→entity uniformly. This keeps the reverse-dep closure, the scoped read-back, and the
refcount query identical across all languages.

Two supporting items:

- **PL/SQL gains `File` nodes + `HAS_FILE`** — the only structural addition; every other
  language already has them.
- **`linkage_key` on externally-referenceable nodes** — mostly already present (the ADR-0004
  cross-repo handle). Its presence lets the *existing* placeholder-caller mechanism
  (`ScopedSpec.satisfied_linkage_keys` → `_affected_by_placeholder_callers`, already built for
  Bash) cover the "newly-satisfiable reference" case for FQN/linkage-keyed languages **without
  adding an `IMPORTS` breadcrumb** — a previously-unresolved reference is an `extern://`
  placeholder keyed by `linkage_key`; when a definition with that key appears, its callers are
  pulled into the emit set.

**Explicitly out of scope:** no new node labels, and **no id-scheme change** — entities keep
their FQN/linkage ids (resolution and cross-repo linkage depend on them). The change is
additive provenance, not re-identification.

### 2. Refcounted delete-or-demote (engine)

Generalize the removal step from `DETACH DELETE by id-prefix` to a per-node decision made from
the graph at the deletion site:

1. Retract the edges the removed/changed file *owned* — found via its `DEFINES`/`DECLARES`
   edges (Tier 2) or its id-prefix scope (Tier 0) — including that file's lateral edges.
2. For each node that lost a contribution, decide its fate locally:
   - **still `DEFINES`/`DECLARES`d by another file** → keep the node (authored-shared
     survivor); recompute any reduced scalar props from the survivors (C2, see §3).
   - **no surviving definition, but a cross-scope inbound reference remains** → **demote** to a
     `resolved:false` placeholder, repointing surviving inbound edges via `MERGE` — the
     existing `demote_cross_repo` (`core/linkage.py`), promoted from the `forget` lifecycle
     path onto the incremental delete path.
   - **neither** → delete.
3. **Aggregation containers** (Tier 1 package/namespace) are garbage-collected when their last
   child edge is gone — the `_reclaim_orphans` refcount, generalized from `extern://`
   placeholders to container nodes.

This subsumes the intra-repo *and* cross-repo delete cases uniformly: the demote decision is
local to the deleted node's neighbourhood, so a cross-repo caller in another repo need not be
reachable by the closure.

**FalkorDB delete semantics force edge-granular retraction.** In FalkorDB, deleting a node
removes *all* its incident edges — there is no `DELETE` vs `DETACH DELETE` distinction, and no
way to drop a node while keeping edges some other file still owns. Two consequences the engine
must respect: (a) a **surviving** authored-shared node must never be node-deleted — retract at
**edge granularity** (`DELETE r` on exactly this file's `DEFINES`/`DECLARES` and lateral edges)
and physically delete the node only when its provenance refcount reaches zero; (b) **demote must
repoint before delete** — MERGE the `resolved:false` placeholder and copy the surviving inbound
edges onto it *first*, then delete the old real node (whose now-redundant edges FalkorDB clears
for free). This MERGE-then-delete order is exactly what `demote_cross_repo` / `_absorb` already
do. The Tier-0 `DETACH DELETE by id-prefix` stays valid only because a file-scoped node is
wholly owned by one file, so nuking its edges harms nothing else.

### 3. Bounded co-contributor closure (engine, C2)

An authored-shared node's own scalar props are a reduction over its contributors (C#'s
partial-class `accessibility` = last-written fragment; C's `defined`/`file_path` = the
definition's). When one contributor changes, the engine pulls that node's **other** contributing
files — found via its `DEFINES`/`DECLARES` edges — into the emit set so the reduction is
recomputed. This closure is bounded by contributor count (a partial class ≈ 2–3 files, a C
function = header + impl), not by repo size. Aggregation containers have constant props and need
no re-derivation.

### 4. `ScopedSpec` contract extension (the future-language guarantee)

The frozen `ScopedSpec` dataclass (`core/incremental_scoped.py`) gains, additively:

- `provenance_rels: (defines_rel, declares_rel)` — the `File→entity` edges (uniform direction).
- `node_kind: label -> {file_owned | aggregation | authored_shared}` — drives delete-by-prefix
  vs GC vs refcount.
- `reduce_props: (node, contributor_raws) -> props` — optional, for authored-shared nodes whose
  scalar props are a reduction.

A **future language** gets delta-sync for free from the shared engine once its ingestor
(a) emits per-entity `DEFINES`/`DECLARES`, (b) declares edge ownership + `node_kind`, (c)
supplies the existing `read_index`/`resolve_incremental` pair, and (d) is syntactic. Deep tiers
declare themselves deep and stay full-recompute.

### 5. Deep-inference tiers stay full-recompute

Jedi and Roslyn (and future deep passes) do **not** get bounded delta. C3 cannot hold for a
black-box per-file extractor: an inferred edge's dependencies are not captured, so affected
edges in untouched files are unfindable. Concretely — untouched `services.py` holds
`run ─CALLS→ Client.dispatch` (Jedi inferred `c: Client`); a commit edits only
`factory.py`'s `make_client() -> Client` to `-> AdminClient`; no name or edge in the graph
predicts `services.py` is affected, so the reverse closure misses it and the delta leaves a
stale edge. This is a property of whole-program inference, not a defect in the design. The
honest options — full-recompute the deep tier (status quo), a conservative import-transitive
superset, or capturing inference-dependency provenance from each tool (a large per-tool effort)
— are deferred to a future ADR. Until then, "delta-sync for all languages" means **the
structural/fast tier of every language**; Python/C# get delta only at fast depth.

## Correctness gate

Same discipline as ADR 0002: every language ships behind the differential oracle
(`test_delta_differential.py`) — the incrementally-synced graph must be node-for-node,
edge-for-edge identical to a full recompute at the target commit, over a corpus exercising
add/modify/delete/rename **and** the authored-shared cases (header/impl split, partial-class
fragment add/remove, reopened-class member move, spec/body edits) in both directions. A wrong
refcount or a missed co-contributor is caught loudly. Full recompute remains the fallback for
every case the incremental path declines.

## Rollout

1. **C first (proof-of-shape).** C needs **zero schema change** — it already has
   `DECLARES`/`DEFINES`. It exercises the entire Tier-2 machine (authored-shared node, refcount,
   demote, prop-reduction) with nothing to build but the engine path + `ScopedSpec`. If the
   oracle stays green for C, the design is validated.
2. **Tier 1 (Java, Go, Rust, Kotlin, PHP)** — add per-entity `DEFINES`, generalize the
   container GC. No refcount, no prop-reduction.
3. **Tier 2 remainder** — C#-treesitter, Ruby, Swift, Julia, then the long pole (C++, PL/SQL,
   which need the provenance/`File`-node layer built first).
4. **Tier 3** — out of scope here; separate ADR if we ever pursue deep-tier delta.

## Consequences

- **Cost collapses to O(change + blast-radius) for the fast tier of every language**, matching
  the ~60× ADR 0002 saw on Python.
- **Provenance becomes load-bearing**, not just diagnostic. The `DEFINES`/`DECLARES` edges must
  be complete and correct; the oracle enforces it.
- **`demote_cross_repo` moves onto a hot path.** It runs on the `forget` lifecycle today; making
  it a per-delete primitive must stay index-backed (inbound-reference count by `id`).
- **The design honestly declines deep-tier delta.** This is a scoping decision, not a gap: the
  goal is met for the structural tier of all 15 git-driven ingestors, and the deep tier's limit
  is a documented property of the problem.
- **Merge-only ingestors (`oraschema`, `sql`, live `oracle`) are unaffected** — they have no git
  path and are outside commit-driven delta by construction.
