# ADR 0002 — Incremental resync (git diff + graph closure)

Status: **Accepted** for the Python `ast` engine — implemented, git-wired end-to-end, and
gated by an equivalence oracle (incremental == full recompute, node/edge-identical) plus an
e2e test proving a real second `ingest_repository` takes the incremental path. Deep engines
(Jedi/Roslyn) and the other languages remain full-recompute (future work).
Date: 2026-09-10.

## Context

Keeping the graph fresh after a source edit is the dominant day-to-day cost, and today it
is wildly disproportionate to the change. Every resync recomputes the *entire* target
state for the changed language — re-parse every file, resolve globally — reads the *entire*
current subgraph back to diff against (`GraphWriter.read_fingerprints`), and writes only
the delta. The freshness benchmark (`benchmarks/freshness/`, see
[the baseline](../../benchmarks/results/freshness-django.json)) quantified it on Django
(2,789 files, 92.7k nodes / 100.7k edges, `ast` engine):

- **Every small edit costs ~constant ~20s**, essentially independent of the edit.
- Of that, **~80% is re-parsing all 2,835 unchanged files** (the `ast` engine has no parse
  cache; only C#/PL-SQL do), ~9% is the whole-graph read-back, ~4% diff — and the **actual
  write is ~3ms**. A one-line comment costs ~20s to do ~0.003s of useful work.

The current design was a deliberate choice (see `core/graph_sync.py` and the C#
`git_sync.py` design note): resolution is **global**, so a source-level file diff "can't
tell which *other*, untouched files need re-examining." Recompute-the-whole-target is
correct by construction; the price is that it's O(repo) on every save.

## Decision

Add an **incremental resync** path that is O(change + blast-radius) instead of O(repo),
driven by two facts we already have but don't use:

1. **git tells us what changed.** The graph already stores `Project.commit_sha`; a
   `git diff <graph_sha>..<target>` (plus `git status --porcelain -u` for the uncommitted
   working tree) gives the changed/added/deleted/renamed files directly — no local parse
   cache, no re-hashing the whole tree.
2. **the graph tells us what's *affected*.** The changed set is not the affected set —
   resolution is global. But the impact is recoverable *from the graph itself*:
   - **reverse-deps:** `(x)-[:CALLS|IMPORTS|EXTENDS|IMPLEMENTS|RETURNS]->(sym)` for symbols
     the change touched → who must be re-resolved even though their files didn't change.
   - **newly-satisfiable refs:** the schema already stores `unresolved_calls` on each
     callable; a newly-added symbol is matched against it to find references that should
     now resolve.

The unchanged files that are *neither* changed nor affected never need re-parsing: the
resolution symbol table (`resolution._Index`: qname→id, packages, class parents) is
**exactly reproducible from the graph** (ids and qualified names are stored precisely, and
never contain volatile inferred types — see `ids.py`). So we seed the index from the graph
for everything outside the blast radius, parse only the blast radius, and **emit only the
blast radius**.

### Algorithm (per language)

1. `old = Project.commit_sha`. If the source isn't git-managed, or `old` is unreachable
   (gc, shallow clone, force-push) → **full resync** (today's path; always the fallback).
2. **Changed set C** from git (committed diff + working-tree status). Classify A/M/D/R.
3. **Parse only C.** Derive the symbols C added and the symbols it removed/renamed.
4. **Affected set A** from the graph: reverse-deps of C's changed/removed symbols +
   `unresolved_calls` matches for C's added symbols. Parse A too (small).
5. **Seed the index from the graph** for every module *not* in C∪A; merge the freshly
   parsed C∪A symbols on top.
6. **Emit only C∪A** against the complete index → the target subgraph for the blast radius.
7. **Scoped read-back + diff + write:** read fingerprints only for the C∪A id-scope (ids
   are module-prefix-nested, so this is a bounded per-module read), `compute_delta`, write,
   bump `commit_sha`.

By construction, any module whose *emitted output* would differ is either changed (in C) or
references a changed symbol (in A) — so emitting only C∪A is complete **iff A is complete**.

### Correctness gate — the benchmark is the oracle

A is a claim, and a wrong A silently leaves the graph stale. So the incremental path ships
behind an **equivalence oracle**: for every scenario in the freshness matrix (rename,
signature change, delete, cross-file, newly-satisfied reference, …) the incremental result
must be **node-for-node, edge-for-edge identical** to a full recompute. Divergence fails
loudly. Incremental becomes a default only once the oracle is green across the matrix.

### Policy

- **Incremental requires a git-managed project.** Non-git (`ingest_directory` over a plain
  `LocalDirectorySource`) → full resync.
- **Uncommitted edits** are handled via `git status --porcelain -u` + `git diff <graph_sha>`
  (an agent that just saved a file usually hasn't committed).
- **Full resync remains** the fallback for every case incremental declines, and the oracle.

## Broken code (mid-edit saves)

A save often lands on syntactically invalid code. Left unhandled this is destructive: a file
that fails to parse yields an empty/partial model, and the delta then **deletes** its
entities (and, incrementally, inbound edges from other files), restoring them only on the
next clean save — so an agent querying the graph can watch a symbol vanish because of an
unrelated typo. (Python's stdlib `ast` is the worst case: all-or-nothing on `SyntaxError`.)

**Decision: last-known-good.** On the incremental path, a changed file that fails to parse is
*dropped from the emit set* rather than emitted — leaving its scope untouched (last-good
subgraph preserved) and its symbols still served to callers via the graph seed. The file
stays "dirty" (uncommitted → already in `dirty_paths`; committed → reappears in the next
`git diff`), so it's re-attempted every sync and self-heals the moment it parses. Broken
files are reported (`LanguageIngestResult.broken_files` → `RepoIngestReport`) as a diagnostic.

This mirrors what LSP servers do in spirit — error-recovering detection + diagnostics +
never destroy valid state — but chooses *freeze-at-last-good* over *persist-the-partial*
because our store is stateful and agent-facing (a stable per-file view beats a flickering
one), and because `ast` can't produce a partial at all. Detection is per-language via
`ScopedSpec.is_broken`: Python `not RawModule.parse_ok` (SyntaxError), tree-sitter
`RawFile.has_error` (`tree.root_node.has_error`). Flipping a tree-sitter language back to
accepting partials is a one-line `is_broken = lambda rf: False`. The full-recompute path
still emits empty/partial for broken files (it has no last-good to keep on a cold ingest) but
now *reports* them too. Verified by `tests/sync/test_broken_code.py` (save-broken → preserved
+ reported → save-fixed → equals a full recompute) for Python and TypeScript.

## Generalization across languages

The engine is language-agnostic and lives in `core/incremental_scoped.py`: the changed-set
handling, the graph affected-closure, the scoped read/delete/diff/write, and the git routing
(commit/checkout via `GitRepoCache`, file/project-save via `WorkingTreeSource`). A language
plugs in a `ScopedSpec`: its schemas, edge classification (containment vs. lateral vs. the
import breadcrumb), id↔scope mapping, and the two genuinely language-specific functions --
reconstruct the symbol table from the graph (`read_index`) and emit a subset against it
(`resolve_incremental`). Robust id→scope resolution uses the longest scope-id prefix (no
fragile marker parsing).

This works only for languages whose ids nest **every entity of a file under one per-file
scope-id prefix**. The roster splits into three tiers:

- **Tier 1 — done: Python, TypeScript.** Both satisfy the invariant (module = file / dotted
  module), have an `IMPORTS` breadcrumb, and reconstruct their symbols from the graph. Both
  run on the shared engine, each behind its own equivalence oracle + trigger tests.
- **Tier 2 — Go, Rust, Java.** Scope is *coarser than a file* (Go package, Java package span
  files; Rust crate-root collapses many files to the project id) and there is **no import/
  dependency edge** (Go/Rust don't persist imports; Java parses but drops them). Needs, per
  language: a per-file ownership handle (a `source_file` on every node, or a file-scoped id
  segment) and an `IMPORTS`/`USES`-style breadcrumb, before the engine applies.
- **Tier 3 — C, C++.** Entities are keyed by **linkage/namespace, not file**, with
  **many-to-one shared nodes** (a fn declared in a `.h`, defined in a `.c` = one node, several
  `File` edges) and renames that change name-keyed ids. The id-prefix scope cannot be reused;
  they need an **edge-scoped, refcounted** variant of the engine (file ownership via
  `File-[:DEFINES|DECLARES|DEFINED_IN]->x`, delete-by-refcount rather than DETACH-by-prefix).

Deep engines (Jedi, Roslyn, tsserver) stay full-recompute regardless of tier (transitive
type ripples; ADR 0002 scope).

## Consequences

- **Cost collapses** from O(repo) to O(change + reverse-dep closure). On the Django comment
  case the ~16s parse and the ~2s read-back both drop to blast-radius scale; the write was
  already milliseconds.
- **The graph becomes load-bearing for resolution**, not just an output. Reverse-dep and
  index-seed queries must be correct and (v2) targeted — v1 reads the full symbol index
  (O(graph), still eliminates the O(repo) parse); v2 narrows to only the names C∪A
  reference.
- **Deep engines (Jedi/Roslyn) are phase 2.** They infer types by reading source and have
  transitive cross-file ripples, so their affected-closure is larger; they stay
  full-recompute until the fast-engine approach is proven.
- **New failure mode: a stale/incomplete A.** Mitigated by the oracle in development and the
  full-resync fallback in production; a periodic full resync can serve as a backstop.

## Implementation notes (Python `ast`)

- **Affected closure = two graph queries.** (a) symbol reverse-deps — `(x)-[:CALLS|EXTENDS|
  IMPLEMENTS|RETURNS]->(sym)` for the changed modules' symbols; (b) **module importers** —
  `(x)-[:IMPORTS]->(m)` for the changed modules. (b) required adding a module-level
  **`IMPORTS` edge** (`Module|Package -> Module|Package`, emitted by `resolution._emit_imports`),
  which is the breadcrumb for a *newly-satisfiable* reference: a module that imports a name
  that only now exists. With it, that case is covered (the equivalence oracle's
  `add_newly_satisfied` scenario passes). Residual gap: importing a *not-yet-existing module*
  (the target node doesn't exist to point an edge at) — would need an `unresolved_imports`
  property; documented, not yet built.
- **Scoped read-back ownership rule.** Lateral edges (`CALLS/EXTENDS/IMPLEMENTS/RETURNS/
  IMPORTS`) are read by **source-in-scope**; the containment spine (`HAS_*`) by
  **target-in-scope** — mirroring which module re-emits each edge, so `compute_delta` never
  deletes an edge whose owner wasn't re-emitted. Removed files are `DETACH DELETE`d by
  id-prefix (which also clears a containment edge sourced at an untouched parent).
- **Routing + fallback.** `git_sync.sync_at` takes the incremental path only when engine is
  `ast` and a fast-depth graph already exists at a *different* state, from one of two ChangeSet
  producers; any error (unreachable base sha on a shallow clone, history rewrite, non-git dir)
  returns `None` and falls back to full recompute, which leaves the graph untouched:
  - **commit / checkout** (`GitRepoCache`): `diff_changed_files(graph_sha, target_sha)` —
    commit-to-commit, rename-aware.
  - **file save / project save** (`WorkingTreeSource`, used by `ingest_directory` when the dir
    is a git working tree): `changed_since(base_commit)` = `git diff <base> + status` (untracked
    included). Two sidecar Project properties support it: `base_commit` (the commit the graph's
    git content is diffed from) and `dirty_paths` (files uncommitted at the last sync). The
    dirty set is what lets a resync notice a file *reverted* back to its committed content —
    `git diff` alone wouldn't flag it, yet the graph's divergence must be undone. A plain
    (non-git) directory has no producer and stays full-resync.
- **Test coverage.** `tests/sync/` exercises all four triggers — git commit, git checkout
  (incl. round-trip), file save, project save — with simple and mixed changes, multiple repos
  in one graph (cross-project isolation), and a structural timing gate; plus the unit
  equivalence oracle and an end-to-end git test. Every correctness test asserts the
  incrementally-synced graph is node/edge-identical to a full recompute of the same state.
- **Every resync query is index-backed.** All queries on the resync path were verified with
  `GRAPH.EXPLAIN` to plan as *Node By Index Scan* (per-label range index on `id`, plus the
  `name` range index for v2's targeted seed) -- never an All Node / Label Scan. This drove one
  fix: the closure's reverse-dep and importer queries were labeling neither endpoint
  (`(x)-[:CALLS]->(sym)` with `sym` unlabeled → all-node scan), so `_symbols_under` now returns
  ids keyed by label and the closure seeks `(sym:Label)` / `(m:Label)` by id. The write path
  (batched `MERGE`/`MATCH ... DETACH DELETE` on the `id` key) uses the unique-constraint index.
- **Cost profile (Django, measured).** full ~18.8s → incremental ~0.31s (~60×). `index_seed`
  (the graph-derived symbol table) was the last O(graph) cost; **v2 narrows it to indexed
  lookups of only the simple names the blast radius references** (`ScopedSpec.referenced_names`
  → `read_index(..., names)` filters the big class/callable reads by `name IN $names`; the
  small package/module lists and inheritance `parents` stay full). Over-collection is safe;
  the equivalence oracle catches any under-collection.
