# ADR 0004 — Cross-repo symbol resolution: external references as upgradable nodes

Status: **Accepted / in progress.** Placeholders + cross-repo reconciliation + `forget` demote
done for the WHOLE language roster (python, c, c++, typescript, java, go, rust, php, julia, c#);
full suite green (672). Remaining: cross-language service→DB (Increment 5). Date: 2026-09-13.

Builds on ADR 0002 (incremental resync), ADR 0003 (multi-repo shared graph, `forget`), and the
cross-ingestor schema contract (shared labels/keys across languages).

## Context

The project's value grows sharply when one graph spans several repos — web services + shared
libraries + the DB schema — and can be *questioned as a whole* ("who calls this?", "what touches
that table?"). Today that whole-system edge is missing: an outbound reference to anything not in
the *current* ingest is thrown away.

- **Python** (`ingestors/python/resolution.py`): an import of a module not in this ingest is
  dropped (`_emit_imports`), and a call the oracle binds outside the project returns `"external"`
  and is skipped (`_resolve_call`). The only residue is a string in the caller's
  `unresolved_calls` list. Provenance ("`charge` came from `billing.core`") is lost.
- **C** (`ingestors/c/resolution.py`) already does the right shape *within* a project: one
  `Function` node per external-linkage symbol, marked `defined:false` when only a prototype is
  seen, `DECLARES` vs `DEFINES`, upgraded in place when the definition is seen. But its ids are
  project-scoped (`c://A/func/charge` vs `c://B/func/charge`), so the same symbol in two repos
  never merges across the boundary; and libc/out-of-project calls are dropped entirely.

We want the graph to represent **every** outbound reference — including system/stdlib/syscalls —
and to *heal* references across repos as more source is ingested.

## Decisions

### 1. An unresolved reference is a first-class node, indistinguishable in shape from a resolved one
A call from resolved function `A` to an unresolved function `B` still yields a `Function` node
for `B` and an ordinary `CALLS` edge `A → B` — the **same label and edge type** as if `B` were
fully resolved. Existing queries keep working unchanged. `B` carries an indicator attribute
(`resolved: false`) and whatever provenance we have (its `module`/namespace and `name`). No
`:External` label, no special edge type. (This generalizes C's existing `defined:false` pattern
to all languages.)

### 2. Real symbols keep project-scoped ids + a `linkage` property; only placeholders are linkage-keyed
A **placeholder** (an unresolved reference) can only be identified by what the import tells the
consumer — the module path and the name — so it is keyed by a **project-independent linkage id**
in a distinct scheme that can never collide with a real node's id:

| lang | placeholder linkage id |
|------|------------------------|
| Python | `extern://python/{dotted.module}/{symbol}` — e.g. `extern://python/billing.core/charge` |
| C | `extern://c/{name}` — external linkage is program-global |
| C++ | `extern://cpp/{namespace::qualified::name}` |
| Java | `extern://java/{fqcn}#{member}` |

A **real, locally-defined** symbol keeps its existing **project-scoped id**
(`python://billing/module/billing.core.func.charge(amount)`) unchanged, and additionally carries
a `linkage` **property** (indexed) holding the same project-independent key. Multiple consumers of
the same missing symbol MERGE onto one placeholder (so `extern://c/malloc` is a single node with
every caller inbound). Linkage keys are name-based (module/class + name), never parameter- or
type-embedded: a consumer can't know callee param names.

*Why not key real nodes by linkage too?* Because merge happens by id at write time: two
independent services that each fully define `app.main.run` locally would then collide into one
node. Project-scoped real ids keep them distinct; the `linkage` property is what crosses the
boundary. **Blast radius is therefore small** — existing single-repo ids and their golden tests
are unchanged; the new work is the placeholder path + a reconciliation step.

### 3. Resolution is a linkage symbol-table lookup; reconciliation upgrades in place; `forget` reverses it
Call/reference resolution gains one step: local project symbol (as today) → else a
**cross-repo lookup by linkage** in the symbol table (which ADR 0002's incremental seed populates
from the graph, so already-ingested repos' symbols are visible) → else a placeholder. So when the
producer is already present, the consumer's edge binds **straight to the real node** — no
placeholder is ever created.

When the producer is ingested *after* a consumer, a small **reconciliation** runs: for each
placeholder whose `linkage` matches a newly-emitted real definition, repoint the placeholder's
inbound edges onto the real node and delete the placeholder. Observ­ably this is exactly the
"upgrade in place" the design calls for — before: `A -[:CALLS]-> (charge {resolved:false})`;
after: `A -[:CALLS]-> (charge {resolved:true, + full attrs, in project billing})`. The consumer
authored nothing new; only the edge target moved (and only for cross-boundary edges, a tiny
fraction). `forget` the producer reverses it: re-materialize a placeholder for any still-referenced
symbol and repoint back; delete the real subgraph. Refcount by inbound references decides
delete-vs-demote (the C/C++-tier machinery ADR 0002 flagged).

### 4. Surface system / built-in / library symbols too
Every unresolved reference becomes a node — so `malloc`, `memcpy`, `fork`, `printf` (C) and
`len`, `open`, `os.fork`, `requests.get` (Python) all appear, each as **one** node with every
caller as an inbound `CALLS` edge. A coarse **`origin`** attribute classifies them so a
never-resolves system symbol is distinguishable from a not-yet-ingested sibling repo:

- `local` — resolved within this ingest.
- `builtin` / `stdlib` — Python, precise: `builtins` namespace / `sys.stdlib_module_names`.
- `libc` / `system` — C: a curated POSIX/libc name set (`malloc`, `free`, `mem*`, `str*`,
  `fork`, `exec*`, `open`, `read`, `write`, `socket`, `printf`, …).
- `external` — unresolved and not known-system → the candidate a future `ingest` upgrades.

This makes "what calls `fork`/`system`/`strcpy`/`eval`/`pickle.loads`" a one-hop query — audit
and security value independent of the multi-repo story. `origin` also lets a UI mute the noisy
`builtin`/`stdlib` fan-out by default.

### 5. Cross-repo edges live in a shared graph; per-repo graphs still gain the external surface
Merging across repos only happens when they're ingested into **one** graph
(`ingest --graph-name system …`) — that shared graph *is* the whole-system view. A default
per-repo graph still benefits: it gains the full outbound surface (stdlib/syscalls/third-party/
unresolved-sibling references) even though nothing upgrades them.

### 6. Collisions are surfaced, not silently merged
A repo's own locally-resolved symbols never become placeholders, so two independent repos that
merely *contain* a same-named module never accidentally merge. The only merge is when a
placeholder's linkage matches a real definition. If **two** repos define the same linkage
(genuine namespace clash / an ambiguous import), record all definers on the node
(`defined_by: [...]`) and flag it rather than picking one.

## Interactions

- **Incremental resync (ADR 0002):** resolution is intrinsic to emission (a write-time MERGE),
  not a batch enrichment — so it stays fresh incrementally. Only the changed+affected set's
  placeholders/edges churn; adding an import creates/merges a placeholder, removing one may orphan
  it (GC by refcount).
- **Cross-language (service → DB):** the same machinery with a SQL linkage vocabulary — a
  function running `SELECT … FROM orders` emits a reference to `sql://table/orders`, which the SQL
  ingestor's `Table` nodes carry. Detecting SQL-in-strings / ORM model↔table mappings is
  heuristic, so this is a **later phase**; the linkage-key design is chosen to accommodate it.

## Consequences / phasing

1. **Python** — *Done.* `linkage` property on real Function/Method/Class; placeholder
   `Function`/`Module` nodes (`extern://…`) + ordinary `CALLS`/`IMPORTS` edges for unresolved
   refs, with `resolved:false`/`origin`/provenance; a missing symbol inside a *project* module is
   treated as intra-project dangling (not external), so nothing stale survives an incremental
   resync. Both engines (ast + jedi) agree; equivalence oracle green; existing project-scoped ids
   unchanged. (`ingestors/python/{ids,resolution}.py`; `tests/ingestors/python/test_{ast,jedi}_engine.py`.)
2. **Cross-repo reconciliation** — *Done.* `core/linkage.py`: `reconcile_linkages` folds
   placeholder↔definer pairs among only the linkages an ingest touched (collected in-memory),
   via index-backed `linkage IN […]` / `id IN […]` seeks — **no graph-wide read** (per the
   no-scan constraint). `_absorb` repoints a placeholder's inbound edges onto the real node
   (`SET e2 = e`) and deletes it; handles both ingest orders. Wired into the full path
   (`git_sync._apply`) and the incremental path (`incremental_scoped`).
3. **Lifecycle `forget` demote** — *Done.* `demote_cross_repo` (reverse of `_absorb`) preserves
   inbound cross-repo references when a producer repo is removed — re-materializes a placeholder
   for each still-referenced symbol and repoints, then deletes. Also fixed a pre-existing
   shared-graph **data-loss bug**: the forget WHERE `id = exact OR (id >= lo AND id < hi)`
   over-matched in FalkorDB (dropped the range upper bound), deleting sibling projects sorting
   after the target; now a pure-range index scan + an exact point-lookup. Two-repo e2e:
   `tests/e2e/test_cross_repo_resolution.py`. *Residual:* consumer re-sync churn (a placeholder is
   re-emitted then re-absorbed each resync) — converges; emit-time binding is a later optimization.
4. **Whole language roster** — *Done.* Every ingestor emits `extern://<lang>/…` placeholders for
   unresolved calls/imports (`resolved:false` + `origin`) and stamps `linkage_key` on real
   externally-visible callables (indexed), then calls `reconcile_linkages` in its write path:
   - c/c++ (native linkage; libc/std `origin`; `extern` prototype in one repo absorbs into the
     definition in another), java/c#/php (method calls; JDK/BCL/builtin `origin`; C# covers both
     tree-sitter and roslyn), go/rust (free fns + typed-receiver methods; stdlib/crate `origin`),
     julia (generic functions; Base `origin`).
   - typescript: external **imports** surface as placeholder Modules (npm/node builtins visible);
     `linkage_key` on callables/types. Cross-repo *symbol* healing is limited by TS's module
     identity (npm package name vs repo-relative file path) — a documented follow-up needing
     package.json/tsconfig path mapping.
   - core generalization: reconcile's canonical = genuine *definition*; absorbable = placeholder
     (`resolved:false`) OR cross-repo declaration (`defined:false`); `_present`-label filter; the
     shared-graph `forget` data-loss bug fixed (see item 3).
5. **Cross-language DB resolution** (§Interactions) — its own phase (not yet started).
