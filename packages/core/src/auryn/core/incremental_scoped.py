"""Generic incremental resync engine for *file-scoped-id* languages (ADR 0002).

This is the language-agnostic core of what Python's ingestor pioneered: instead of an
O(repo) full recompute, move a project's subgraph to a new state by (1) taking the changed
files from git, (2) asking the GRAPH which untouched files are affected, (3) re-parsing and
re-emitting ONLY that blast radius against a symbol table seeded from the graph, and (4)
reconciling with a *scoped* read-back + delta.

It works for any language whose id scheme nests every entity of a file under a single
per-file scope-id prefix (Python module/package, TypeScript module, …). Languages whose
entities are keyed by something coarser than a file (Go package, Java package, C linkage,
C++ namespace) do NOT satisfy that invariant and need a different engine -- see ADR 0002.

Each language supplies a `ScopedSpec`: its schemas, edge classification, id↔scope mapping,
parse function, and the two genuinely language-specific pieces -- reconstructing its symbol
table from the graph (`read_index`) and emitting a subset against it (`resolve_incremental`).
Everything else -- the closure, the scoped read/delete/diff/write, and the git routing -- is
here and shared.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from auryn.core import progress
from auryn.core.graph_client import SHARED_NODE_LABEL, GraphWriter, semantic_label
from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.core.graph_sync import compute_delta, edge_key, node_key, unstamped
from auryn.core.instrument import phase

logger = logging.getLogger(__name__)


@dataclass
class ChangeSet:
    """What changed between the graph's state and the target, as repo-relative paths. A rename
    is (old, new): a delete of old + an add of new for a path-derived id scheme."""

    modified: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    renamed: list[tuple[str, str]] = field(default_factory=list)

    def present_paths(self) -> list[str]:
        return list(dict.fromkeys(self.modified + self.added + [new for _, new in self.renamed]))

    def removed_paths(self) -> list[str]:
        return list(dict.fromkeys(self.deleted + [old for old, _ in self.renamed]))


@dataclass
class ScopedSyncStats:
    """Duck-compatible with each ingestor's own SyncStats (same fields) so it flows straight
    into LanguageIngestResult."""

    nodes_added: int = 0
    nodes_updated: int = 0
    nodes_removed: int = 0
    edges_added: int = 0
    edges_updated: int = 0
    edges_removed: int = 0
    commit_sha: str = ""
    elapsed_seconds: float = 0.0
    files_parsed: int = 0
    files_cached: int = 0
    broken_files: list[str] = field(default_factory=list)  # changed files that failed to parse (last-good kept)


@dataclass(frozen=True)
class ScopedSpec:
    """Everything the engine needs from a language. The three hard, language-specific pieces
    are `parse`, `read_index`, and `resolve_incremental`; the rest is small declarative glue."""

    language: str
    node_schemas: tuple  # the real NodeSchema list (for setup_schema's indexes/constraints)
    node_labels: tuple[str, ...]
    file_suffixes: tuple[str, ...]
    excluded_dir_names: frozenset[str]
    # Edge classification (see read_scoped_fingerprints for why the source/target ownership
    # split matters). `symbol_ref_rels` point at a symbol node (walked for the reverse-dep
    # closure); `import_rel` points module→module (the file-dependency breadcrumb) or None.
    symbol_ref_rels: tuple[str, ...]
    lateral_rels: tuple[str, ...]        # source-owned edges (incl. symbol_ref + import)
    containment_rels: tuple[str, ...]    # target-owned structural spine
    targetable_labels: tuple[str, ...]   # labels a symbol_ref edge can point at
    import_rel: str | None
    import_target_labels: tuple[str, ...]  # labels the import_rel edge can point at
    project_id: Callable[[str], str]                      # project_name -> proj_id
    scope_id_for_path: Callable[..., str]                 # (proj_id, path[, ctx]) -> owning id prefix
    scope_key_for_path: Callable[..., str]                # (path[, ctx]) -> stable scope key
    parse: Callable[..., object]                          # (bytes, path[, ctx]) -> raw file model
    is_broken: Callable[[object], bool]                   # raw -> did the file fail to parse?
    referenced_names: Callable[[object], set[str]]        # raw -> simple names it references cross-file
    read_index: Callable[..., object]                     # (writer, proj_id, exclude keys, ref names) -> index
    resolve_incremental: Callable[[list, str, object], Iterable]  # (emit raws, proj_id, index) -> items
    project_node: Callable[..., GraphNode]               # (project_name, repo_uri, proj_id, target_sha, ref)
    # Optional: build a per-sync "path context" from the full file list (e.g. Python's set of
    # `__init__.py`-bearing package dirs, for import-root detection). When set, the engine computes
    # it ONCE from every path at target_sha and passes it as a trailing arg to scope_id_for_path /
    # scope_key_for_path / parse -- so all three, and the full-recompute engine (which builds the
    # same context from the same complete list), map a path to an identical scope key/id.
    build_path_context: Callable[[list[str]], object] | None = None
    child_sep: str = "."  # what separates a scope id from a child entity segment in an id
    # Optional: among the files matching `file_suffixes`, keep only those this language actually OWNS,
    # by (path, lazy-blob-reader). The sole user is C: `.h` is in its suffixes but a C++-content `.h`
    # belongs to the C++ ingestor (see core.cpp_headers) -- without this, C's incremental resync would
    # re-parse a changed C++ header into mangled `c://` nodes that duplicate C++'s full-recompute. Must
    # match the language's full-ingest walk, so a header's owner never differs between full and delta.
    # None (the default) owns every suffix match -- correct for every language with unambiguous suffixes.
    claims_path: Callable[[str, Callable[[], bytes]], bool] | None = None
    # Optional: for languages that resolve references by BARE NAME across the whole project with no
    # dependency edge (Bash: a function is callable once its file is `source`d, and our resolver is
    # even more generous), a changed file that newly DEFINES a name can satisfy a placeholder some
    # untouched file already points at -- but that caller points at the `extern://` placeholder, not
    # the (not-yet-existing) real symbol, so neither the reverse-dep nor the import closure finds
    # it. Given the parsed emit raws, this returns the placeholder linkage keys those files' NEW
    # definitions could satisfy; the engine then pulls the placeholders' callers into the emit set
    # so they re-resolve to the real definition (== a full recompute). None (the default) disables
    # it entirely -- languages whose cross-file references always ride an import/symbol edge (TS,
    # Python) are fully covered by the two standard closures and set nothing.
    satisfied_linkage_keys: Callable[[list], set[str]] | None = None
    # Authored-shared (Tier-2, ADR 0005): a non-empty `provenance_rels` switches routing to the
    # provenance-scoped engine (`incremental_sync_authored`). There, a file's contribution is the
    # set of program-global nodes it DEFINES/DECLARES (shared across files, ids NOT nested under the
    # file), so the scoped read-back, the co-contributor closure and the refcounted delete-or-demote
    # all key off these edges rather than an id-prefix. Empty (the default) = Tier-0 id-prefix engine
    # (Python/TypeScript/Bash), left completely unchanged.
    provenance_rels: tuple[str, ...] = ()
    linkable_labels: tuple[str, ...] = ()  # labels carrying `linkage_key` -> demote candidates on delete
    # (raw_file, proj_id) -> [(node_id, label)] of the program-global symbols the file DEFINES/DECLARES,
    # computed cheaply from the parse (same id functions the emitter uses). Enables the v2 blast-radius
    # narrowing: only symbols ADDED/REMOVED by a changed file (id diff vs the graph) drive the
    # reverse-dep closure -- editing a body or adding a symbol pulls no callers (ADR 0002-v2 / 0005).
    defined_symbol_ids: Callable[[object, str], list[tuple[str, str]]] | None = None
    # The subset of `provenance_rels` that owns a node's DEFINITION data (props, lateral edges,
    # children). The rest (e.g. C's DECLARES) own only the provenance edge itself -- a file that
    # merely DECLARES a symbol must not drag that symbol's definition-owned edges into the read-back
    # (ADR 0005: a shared header declaring hundreds of symbols would otherwise strip their CALLS).
    # Defaults to `provenance_rels` (declaration == definition) when a language doesn't split them.
    definition_rels: tuple[str, ...] = ()
    # Aggregation-tier labels (Module/Package/Namespace) whose node props are FULLY id-derived, so a
    # multiply-authored one reduces to the SAME node regardless of which author "wins" -- these are
    # excluded from the transitive co-DEFINER closure (winner reduction), which for them is pure cost.
    # They stay in touched_defined (so GC still deletes an orphaned one) and in the co-CONTRIBUTOR
    # closure (so removing one author re-checks its siblings). Crucial for Rust, whose crate-root
    # collapse conflates every file's inline `mod tests {}` into one node authored by dozens of files:
    # without this, editing any one of them drags the whole cluster in (ADR 0005). Requires that the
    # excluded label carry NO author-dependent prop -- Rust's Module dropped `is_declaration` for this.
    # Empty (the default) leaves the closure query byte-identical for every other language.
    aggregation_labels: tuple[str, ...] = ()
    # Optional: a newly ADDED definition can DISPLACE an existing reference -- not via a placeholder
    # (that's `satisfied_linkage_keys`) but by out-biasing a real target already bound. Java's module
    # tier makes this reachable: two Gradle modules may share a package, so `app.Client` can bind to
    # `modA.app.Helper` until `modB` adds its own `app.Helper`, which a full resolve prefers (same
    # module) -- but neither standard closure revisits Client (it's no one's reverse-dep). Given the
    # parsed emit raws, this returns the File ids whose references the emit set's added symbols may
    # displace; the engine pulls them into the emit set so they re-resolve exactly as a full recompute
    # would. None (the default) disables it -- a language whose resolution has no cross-scope name
    # bias (every other ingestor) is fully covered by the standard closures and sets nothing.
    displaced_caller_files: Callable[[GraphWriter, str, list], set[str]] | None = None
    # Optional: containment rels by which a TYPE owns MEMBER NODES that may be authored in a DIFFERENT
    # file than the type's declaration -- Rust `impl` blocks / Go methods on a type, which live in
    # sibling files. The co-member closure (incremental_sync_authored) pulls those member-definer files
    # into the emit set so a touched type's cross-file methods (and the IMPLEMENTS/EXTENDS edges carried
    # BY those member-bearing impl blocks) are re-authored together; otherwise the provenance-scoped
    # read-back claims a cross-file member the emit set can't reproduce and compute_delta deletes it (an
    # incremental != full-recompute data loss, seen on a real multi-crate Rust repo). Empty (the
    # default) is a no-op -- correct for languages whose type members are single-file (C struct fields,
    # Java class methods: the member's definer IS the type's declaring file).
    type_member_rels: tuple[str, ...] = ()
    # Optional: the REVERSE of `type_member_rels` -- the member->owner-type direction. When a MEMBER's
    # definer file is in the emit set, pull the file that DEFINES its owning TYPE. Needed by C++, whose
    # out-of-line method definitions (`void Shape::area(){..}` in a .cc) are authored in a DIFFERENT file
    # than the class body (in a .h): editing the .cc must re-author Shape's whole subgraph (so its other
    # methods/fields aren't dropped, and the method merges its in-class declaration), exactly as editing
    # the .h pulls the .cc via `type_member_rels` (the forward direction). Set to the same rel as
    # `type_member_rels` (e.g. HAS_METHOD). Empty (default) is a no-op -- correct for languages whose
    # members are single-file (a member's file IS its type's file: C/Java/Go/Rust have no .h/.cc split).
    member_owner_rels: tuple[str, ...] = ()
    # Optional: source-owned type->type edges (Rust IMPLEMENTS) that a file OTHER than the source type's
    # declaring file may author -- `impl Trait for Type` can live in ANY file of the crate that defines
    # Type OR Trait (the orphan rule), and a METHODLESS impl leaves no member node to attribute it to its
    # file. When a type is pulled into the emit set only transitively (it shares a file with a genuinely-
    # changed type's cross-file method), the provenance-scoped read-back claims its IMPLEMENTS edge, but
    # the emit set never re-authors it -> compute_delta would drop it (a `_foreign_defined`-style data loss,
    # but for an EDGE, which that node-level filter can't catch). These edges therefore carry an `_src_file`
    # property (their authoring file, stamped by the ingestor); the read-back excludes any such edge whose
    # `_src_file` is NOT in the emit set -- so an impl authored by a non-emit file (co-located with the
    # trait, OR in a third file that defines neither type nor trait) is kept in place, while an impl whose
    # own file IS re-emitted (edit/removal) is still diffed normally. Robust without any closure pull.
    # Empty (the default) is a no-op -- correct for every language whose type-level edges are authored at
    # the source's declaration (Go EMBEDS, C/Java/C++ inheritance, Rust EXTENDS): there the source's file
    # is always in the emit set when the source is claimed, so the edge is re-authored, never orphaned.
    type_impl_rels: tuple[str, ...] = ()


def _range(prefix: str) -> tuple[str, str]:
    return prefix, prefix + chr(0xFFFF)


def _claimed_files(source, target_sha: str, spec: "ScopedSpec") -> dict[str, str]:
    """`{path: blob_sha}` for every file matching `spec.file_suffixes` that this language OWNS. When
    `spec.claims_path` is set it disowns a suffix match another language claims by content -- C's `.h`
    that is actually a C++ header (see core.cpp_headers). Keeps the incremental file set identical to the
    full-ingest walk, so a header's owning ingestor never flips between full and delta."""
    out: dict[str, str] = {}
    for suffix in spec.file_suffixes:
        for path, blob_sha in source.list_files(target_sha, suffix, spec.excluded_dir_names):
            if spec.claims_path and not spec.claims_path(path, lambda b=blob_sha: source.read_blob(b)):
                continue
            out[path] = blob_sha
    return out


def _owns(node_id: str, scope_id: str, scope_ids: frozenset[str], sep: str) -> bool:
    """True iff `scope_id` is the LONGEST scope that owns `node_id` -- the node is the scope's own
    node, or a descendant with no deeper scope sitting between them. Mirrors `_owning_key`'s
    longest-prefix rule so a parent package's scoped read never swallows a nested sub-package's
    nodes (whose ids continue the parent's dotted path, e.g. `pkg` vs `pkg.sub`)."""
    if node_id == scope_id:
        return True
    if not node_id.startswith(scope_id + sep):
        return False
    if node_id in scope_ids:  # the node is itself a deeper scope's own node
        return False
    tail = node_id[len(scope_id) + len(sep):]
    i = tail.find(sep)
    while i != -1:
        if scope_id + sep + tail[:i] in scope_ids:  # a deeper scope owns it
            return False
        i = tail.find(sep, i + len(sep))
    return True


# ── the engine ───────────────────────────────────────────────────────────────────────
def incremental_sync(
    writer: GraphWriter,
    spec: ScopedSpec,
    source,
    target_sha: str,
    changes: ChangeSet,
    *,
    project_name: str,
    repo_uri: str,
    ref: str | None = None,
) -> ScopedSyncStats:
    """Move `project_name`'s subgraph to `target_sha` by processing only `changes` + their
    graph-derived blast radius. Correctness (== a full recompute) is enforced per-language by
    an equivalence oracle."""
    started = time.monotonic()
    proj_id = spec.project_id(project_name)
    writer.setup_schema(list(spec.node_schemas))

    # Current file set + the id/scope bookkeeping the closure and reads rely on.
    path_to_hash = _claimed_files(source, target_sha, spec)

    # Per-sync path context (e.g. Python's package dirs for import-root detection), built ONCE from
    # the complete file list -- the same list the full-recompute engine uses -- so scope key/id and
    # parse all map a path identically in both engines. Languages without the hook keep their plain
    # (path)/(proj_id, path)/(bytes, path) signatures via the None branch.
    ctx = spec.build_path_context(list(path_to_hash)) if spec.build_path_context else None
    if ctx is None:
        scope_id_for, scope_key_for, parse_file = spec.scope_id_for_path, spec.scope_key_for_path, spec.parse
    else:
        scope_id_for = lambda proj, p: spec.scope_id_for_path(proj, p, ctx)  # noqa: E731
        scope_key_for = lambda p: spec.scope_key_for_path(p, ctx)            # noqa: E731
        parse_file = lambda b, p: spec.parse(b, p, ctx)                      # noqa: E731

    scope_id_to_key = {scope_id_for(proj_id, p): scope_key_for(p) for p in path_to_hash}
    key_to_path = {scope_key_for(p): p for p in path_to_hash}

    # A present file this language no longer owns (disowned by `claims_path`) is folded into the removal
    # set so its stale nodes don't survive alongside the new owner's graph. Empty unless a spec sets
    # `claims_path` (see the authored engine for the C/C++ `.h` case).
    disowned = [p for p in changes.present_paths() if p not in path_to_hash]
    removed_paths = changes.removed_paths() + disowned
    removed_scope_ids = [scope_id_for(proj_id, p) for p in removed_paths]
    changed_present = [p for p in changes.present_paths() if p in path_to_hash]
    changed_scope_ids = [scope_id_for(proj_id, p) for p in changed_present]
    changed_keys = {scope_key_for(p) for p in changed_present}
    removed_keys = {scope_key_for(p) for p in removed_paths}

    # 1. Affected closure from the graph: reverse-deps of the changed/removed symbols, plus
    #    importers of the changed/removed files (catches a reference that only now resolves).
    #    Every query here is a per-label id lookup so it hits a range index (verified by EXPLAIN).
    with phase("closure"):
        symbols_by_label = _symbols_under(writer, spec, changed_scope_ids + removed_scope_ids)
        affected_keys = _affected_by_symbol_ref(writer, spec, symbols_by_label, scope_id_to_key)
        affected_keys |= _affected_by_import(writer, spec, changed_scope_ids + removed_scope_ids, scope_id_to_key)
    affected_paths = [
        key_to_path[k] for k in affected_keys
        if k in key_to_path and k not in changed_keys and k not in removed_keys
    ]

    # 2. Parse ONLY the emit set (changed + affected present files). A file that fails to parse
    #    (broken/mid-edit code) is DROPPED here rather than emitted: leaving it out of the emit
    #    set preserves its last-good subgraph (its scope is never re-read/deleted) and keeps its
    #    symbols available to callers via the graph seed. It stays "dirty" and is re-attempted
    #    on the next sync. See ADR 0002.
    all_emit_paths = list(dict.fromkeys(changed_present + affected_paths))
    with phase("parse"):
        parsed = []
        for i, p in enumerate(all_emit_paths, 1):
            progress.log_file(logger, p, i, len(all_emit_paths))
            parsed.append((p, parse_file(source.read_blob(path_to_hash[p]), p)))

    # Placeholder-satisfaction closure (bare-name languages only, e.g. Bash): a file we just parsed
    # may DEFINE a name that an untouched file already calls via an `extern://` placeholder. Those
    # callers point at the placeholder, not the (previously non-existent) real symbol, so neither
    # standard closure found them. Pull them in and parse them so they re-resolve to the real
    # definition -- and the now-orphaned placeholder is reclaimed after the write, exactly as a full
    # recompute would leave it. Disabled (no query) unless the spec opts in.
    if spec.satisfied_linkage_keys is not None:
        satisfied = spec.satisfied_linkage_keys([r for _, r in parsed])
        emit_keys_so_far = {scope_key_for(p) for p in all_emit_paths}
        extra_keys = _affected_by_placeholder_callers(writer, spec, satisfied, scope_id_to_key)
        extra_paths = [
            key_to_path[k] for k in extra_keys
            if k in key_to_path and k not in changed_keys and k not in removed_keys and k not in emit_keys_so_far
        ]
        with phase("parse"):
            for i, p in enumerate(extra_paths, 1):
                progress.log_file(logger, p, len(all_emit_paths) + i, len(all_emit_paths) + len(extra_paths))
                parsed.append((p, parse_file(source.read_blob(path_to_hash[p]), p)))
        all_emit_paths = all_emit_paths + extra_paths

    broken_files = sorted(p for p, r in parsed if spec.is_broken(r))
    ok = [(p, r) for p, r in parsed if not spec.is_broken(r)]
    emit_paths = [p for p, _ in ok]
    emit_raws = [r for _, r in ok]

    # 3. Seed the symbol table from the graph for every OTHER file (broken files included, so
    #    their last-good symbols keep resolving) -- but only the symbols the blast radius could
    #    actually reference (by simple name), not the whole project (v2).
    emit_keys = {scope_key_for(p) for p in emit_paths}
    ref_names: set[str] = set()
    for r in emit_raws:
        ref_names |= spec.referenced_names(r)
    with phase("index_seed"):
        index = spec.read_index(writer, proj_id, emit_keys | removed_keys, ref_names)

    # 4. Emit the target subgraph for ONLY the emit set (+ the Project node).
    with phase("resolve"):
        target_items: list = [spec.project_node(project_name, repo_uri, proj_id, target_sha, ref)]
        target_items.extend(spec.resolve_incremental(emit_raws, proj_id, index))

    # 5a. Delete removed files' subgraphs outright (DETACH also clears containment edges
    #     sourced at an untouched parent, which a scoped diff wouldn't see). Capture the extern
    #     placeholders those files referenced FIRST -- the DETACH DELETE drops their edges silently
    #     (they never reach the delta below), so a placeholder a deleted file was the last caller of
    #     must be reclaimed explicitly.
    removed_nodes = 0
    orphan_candidates: set[tuple[str, str]] = set()
    with phase("write"):
        writer.merge_nodes([unstamped(target_items[0])])  # no commit claimed from the first mutation on
        for scope_id in removed_scope_ids:
            orphan_candidates |= _externs_referenced_by(writer, spec, scope_id)
            removed_nodes += _delete_subgraph(writer, spec, scope_id)

    # 5b. Scoped read-back + diff + write for the emit set.
    emit_scope_ids = [scope_id_for(proj_id, p) for p in emit_paths]
    with phase("read_fingerprints"):
        cur_nodes, cur_edges = read_scoped_fingerprints(
            writer, spec, emit_scope_ids, frozenset(scope_id_to_key)
        )
    delta = compute_delta(cur_nodes, cur_edges, target_items)
    for _rel, _from_label, _from_key, to_label, to_key in delta.edges_to_remove:
        tid = str(to_key.get("id", ""))
        if tid.startswith("extern://") and to_label in spec.node_labels:
            orphan_candidates.add((tid, to_label))
    with phase("write"):
        writer.apply_delta(delta)
        # Cross-repo reconciliation (ADR 0004): heal placeholder<->definer pairs among the
        # linkages this resync touched -- so a save/commit that adds a cross-repo call binds it,
        # and one that removes the last such call lets the placeholder fall away. Targeted index
        # seeks over the emit-set linkages, no scan.
        from auryn.core.linkage import collect_linkages, reconcile_linkages

        reals, placeholders = collect_linkages(target_items)
        reconcile_linkages(writer, reals, placeholders)
        # Reclaim placeholders no edge points at anymore (see _reclaim_orphans): keeps
        # incremental == full recompute for `extern://` nodes, which a scoped diff can't see.
        reclaimed = _reclaim_orphans(writer, orphan_candidates)

    nodes_added = sum(1 for n in delta.nodes_to_upsert if node_key(n) not in cur_nodes)
    edges_added = sum(1 for e in delta.edges_to_upsert if edge_key(e) not in cur_edges)
    return ScopedSyncStats(
        nodes_added=nodes_added,
        nodes_updated=len(delta.nodes_to_upsert) - nodes_added,
        nodes_removed=len(delta.nodes_to_remove) + removed_nodes + reclaimed,
        edges_added=edges_added,
        edges_updated=len(delta.edges_to_upsert) - edges_added,
        edges_removed=len(delta.edges_to_remove),
        commit_sha=target_sha,
        elapsed_seconds=time.monotonic() - started,
        files_parsed=len(emit_raws),
        files_cached=len(path_to_hash) - len(emit_raws),
        broken_files=broken_files,
    )


# ── authored-shared engine (Tier-2, ADR 0005) ─────────────────────────────────────────
def incremental_sync_authored(
    writer: GraphWriter,
    spec: ScopedSpec,
    source,
    target_sha: str,
    changes: ChangeSet,
    *,
    project_name: str,
    repo_uri: str,
    ref: str | None = None,
) -> ScopedSyncStats:
    """Incremental resync for AUTHORED-SHARED languages (ADR 0005), where ids are program-global
    (keyed by linkage/FQN, not by file) and one node may be co-authored by several files (a C
    function DECLARED in headers and DEFINED in a .c; a partial class; a namespace). A file's
    contribution is defined by its ``DEFINES``/``DECLARES`` provenance edges, so this differs from
    the Tier-0 id-prefix engine in three ways:

      1. a **co-contributor closure** pulls every OTHER file that defines/declares a touched shared
         node into the emit set, so the node is fully re-authored (its reduced props re-derive, and
         it survives iff a surviving author re-emits it);
      2. a **provenance-scoped read-back** (`read_authored_fingerprints`) reads current state via
         the provenance edges, not an id-prefix range;
      3. a **refcount delete-or-demote**: a shared node that lost its last definer is deleted, or
         -- if a cross-repo consumer still references it -- demoted to an ``extern://`` placeholder
         first (FalkorDB drops all of a node's edges on delete), so the consumer keeps an
         (unresolved) reference. Correctness (== full recompute) is the differential oracle's job.
    """
    started = time.monotonic()
    proj_id = spec.project_id(project_name)
    writer.setup_schema(list(spec.node_schemas))

    path_to_hash = _claimed_files(source, target_sha, spec)

    # Authored-shared languages have no per-sync path context (no import-root inference).
    scope_id_for, scope_key_for, parse_file = spec.scope_id_for_path, spec.scope_key_for_path, spec.parse
    scope_id_to_key = {scope_id_for(proj_id, p): scope_key_for(p) for p in path_to_hash}
    key_to_path = {scope_key_for(p): p for p in path_to_hash}

    # A present (modified/added) file that this language no longer OWNS -- a C `.h` edited to add C++
    # syntax, which `claims_path` now routes to the C++ ingestor -- must have its OLD nodes deleted, else
    # they survive next to the other ingestor's fresh graph (incremental != full). git reports it as a
    # modification, not a delete, so fold these "disowned" paths into the removal set. Empty unless a spec
    # sets `claims_path` (only C), since otherwise every suffix match is in path_to_hash.
    # KNOWN RESIDUAL (narrow; PR #16 review): a DECLARES-only program-global symbol -- a function
    # prototype or forward-declared type that is never DEFINED anywhere in-project -- whose last declarer is
    # the removed/disowned file is not reclaimed here: the gone-set is DEFINES-based (definition_rels), so a
    # symbol with zero DEFINES is never in it, and its (now edge-less) node lingers until a full re-ingest.
    # Rare (a prototype/forward-decl of a symbol defined only in another repo / a flipped header); a robust
    # fix is a provenance-refcount sweep over DECLARES too, deferred.
    disowned = [p for p in changes.present_paths() if p not in path_to_hash]
    removed_paths = changes.removed_paths() + disowned
    removed_file_ids = [scope_id_for(proj_id, p) for p in removed_paths]
    changed_present = [p for p in changes.present_paths() if p in path_to_hash]
    changed_file_ids = [scope_id_for(proj_id, p) for p in changed_present]
    changed_keys = {scope_key_for(p) for p in changed_present}
    removed_keys = {scope_key_for(p) for p in removed_paths}

    # Program-global nodes the changed+removed files CURRENTLY define/declare (captured before any
    # deletion). touched_defined (DEFINES only) drives the orphan sweep (5c) and the rename read-back
    # seed; a symbol merely DECLARED by a changed/removed header is not "gone" if still defined
    # elsewhere.
    touched_prog = _defined_nodes(writer, spec, changed_file_ids + removed_file_ids)
    touched_defined = _defined_nodes(writer, spec, changed_file_ids + removed_file_ids,
                                     rels=spec.definition_rels or spec.provenance_rels)
    touched_defined_ids = [nid for nid, _ in touched_defined]

    # v2 blast-radius narrowing (ADR 0002-v2 / 0005): parse the changed files up front and diff their
    # NEW symbol ids against the graph's OLD ones. Only a REMOVED id (or a whole removed file's
    # symbols) can affect callers -- a persisting name is still resolved, and a newly ADDED name has
    # no existing real callers (a stale extern:// caller is handled by the placeholder closure). So
    # the reverse-dep + co-contributor closures target only removed symbols: editing a body or adding
    # a symbol pulls no callers.
    with phase("parse"):
        changed_parsed = [(p, parse_file(source.read_blob(path_to_hash[p]), p)) for p in changed_present]
    if spec.defined_symbol_ids is not None:
        new_ids = {nid for _, r in changed_parsed for nid, _ in spec.defined_symbol_ids(r, proj_id)}
        removed_syms = [(nid, lbl) for nid, lbl in _defined_nodes(writer, spec, changed_file_ids) if nid not in new_ids]
        removed_syms += _defined_nodes(writer, spec, removed_file_ids)
    else:
        removed_syms = touched_prog  # no hook -> conservative (all touched), pre-v2 behaviour

    # 1. Closure: reverse-deps of the REMOVED symbols (callers re-resolve -> drop/demote), co-contributors
    #    of removed symbols (a removed definition may leave a header declaration to re-emit), importers,
    #    and the transitive co-DEFINER closure (multi-program conflated nodes re-reduce to the full
    #    winner). A caller node is program-global, so reverse-dep results map to their DEFINING file.
    with phase("closure"):
        # Aggregation nodes (Package/Module) are GC'd by refcount after the write (see
        # _reclaim_orphaned_aggregation), NOT by pulling their co-definers: a shared parent like Java's
        # `com` prefix is authored by the whole project, so pulling every co-definer just to confirm it
        # survives would drag the entire repo into a one-file delete. Exclude them from the reverse-dep +
        # co-contributor closures (they carry no symbol-ref edges, so this only skips useless work).
        symbols_by_label: dict[str, list[str]] = {}
        for nid, lbl in removed_syms:
            if lbl not in spec.aggregation_labels:
                symbols_by_label.setdefault(lbl, []).append(nid)
        removed_sym_ids = [nid for nid, lbl in removed_syms if lbl not in spec.aggregation_labels]
        aff_files = _caller_files(writer, spec, symbols_by_label)
        aff_files |= _cocontributor_file_ids(writer, spec, removed_sym_ids)
        affected_keys = {scope_id_to_key[f] for f in aff_files if f in scope_id_to_key}
        affected_keys |= _affected_by_import(writer, spec, changed_file_ids + removed_file_ids, scope_id_to_key)
        # Transitive co-DEFINER closure (ADR 0005 #2): pull every file that co-defines a symbol an
        # emit file defines, so a multiply-defined node re-reduces to the same winner as a full
        # recompute. Bounded fixpoint; a no-op unless something is multiply-defined.
        if spec.definition_rels:
            key_to_sid = {k: sid for sid, k in scope_id_to_key.items()}
            removed_set = set(removed_file_ids)
            emit_ids = (set(changed_file_ids) | {key_to_sid[k] for k in affected_keys if k in key_to_sid}) - removed_set
            # Monotonic fixpoint: emit_ids only grows and is bounded by the file count, so it always
            # terminates -- no arbitrary round cap that could truncate a long cross-file chain and leave
            # the last type's impl file unpulled (PR #13 review). Cross-file IMPLEMENTS edges need no
            # closure pull at all: they carry an `_src_file` provenance stamp and are kept in place by the
            # read-back's foreign-edge filter (see read_authored_fingerprints / ScopedSpec.type_impl_rels),
            # which also covers the orphan impl in a THIRD file no closure query could have found.
            while True:
                # co-DEFINER (same node, multi-file) + co-MEMBER (a type's cross-file method-definer
                # files). Co-member seeds with removed_set too: when a type's DECLARATION file is deleted
                # (moved to a new file, same id), the graph still links the type to the OLD file, so the
                # old file is the only seed that reaches the (unchanged) member files. `- removed_set`
                # after keeps those deleted files out of the emit set itself -- they are lookup seeds only.
                seed = emit_ids | removed_set
                new = (_codefiner_file_ids(writer, spec, emit_ids)
                       | _comember_file_ids(writer, spec, seed)
                       | _comember_owner_file_ids(writer, spec, seed)) - emit_ids - removed_set
                if not new:
                    break
                emit_ids |= new
                affected_keys |= {scope_id_to_key[f] for f in new
                                  if f in scope_id_to_key and scope_id_to_key[f] not in changed_keys}
    affected_paths = [
        key_to_path[k] for k in affected_keys
        if k in key_to_path and k not in changed_keys and k not in removed_keys
    ]
    # 2. Parse the affected files (changed already parsed above); drop broken (last-good preserved).
    all_emit_paths = list(dict.fromkeys(changed_present + affected_paths))
    with phase("parse"):
        parsed = changed_parsed + [(p, parse_file(source.read_blob(path_to_hash[p]), p)) for p in affected_paths]

    # 2b. Placeholder-satisfaction closure (bare-name resolution, e.g. C external calls): a newly
    #     DEFINED name may satisfy an extern:// placeholder an untouched caller already points at.
    if spec.satisfied_linkage_keys is not None:
        satisfied = spec.satisfied_linkage_keys([r for _, r in parsed])
        seen = {scope_key_for(p) for p in all_emit_paths}
        cand_files = _placeholder_caller_files(writer, spec, satisfied)
        extra = [
            key_to_path[scope_id_to_key[f]] for f in cand_files
            if f in scope_id_to_key and scope_id_to_key[f] in key_to_path
            and scope_id_to_key[f] not in changed_keys and scope_id_to_key[f] not in removed_keys
            and scope_id_to_key[f] not in seen
        ]
        with phase("parse"):
            parsed.extend((p, parse_file(source.read_blob(path_to_hash[p]), p)) for p in extra)
        all_emit_paths = all_emit_paths + extra

    # 2c. Displacement closure: an emit file's newly-ADDED definition may out-bias a REAL target an
    #     untouched file is already bound to (a same-name symbol in another scope the full resolve now
    #     prefers). Unlike 2b these callers point at a real node, not an extern:// placeholder, so the
    #     standard closures miss them. Pull the displaced callers' files so they re-resolve to the
    #     newly-preferred target (== full recompute). No-op unless an added symbol actually shadows.
    if spec.displaced_caller_files is not None:
        seen = {scope_key_for(p) for p in all_emit_paths}
        disp_files = spec.displaced_caller_files(writer, proj_id, [r for _, r in parsed])
        extra = [
            key_to_path[scope_id_to_key[f]] for f in disp_files
            if f in scope_id_to_key and scope_id_to_key[f] in key_to_path
            and scope_id_to_key[f] not in changed_keys and scope_id_to_key[f] not in removed_keys
            and scope_id_to_key[f] not in seen
        ]
        with phase("parse"):
            parsed.extend((p, parse_file(source.read_blob(path_to_hash[p]), p)) for p in extra)
        all_emit_paths = all_emit_paths + extra

    # 2d. Re-close the post-step-1 closures over the files 2b/2c just added. Those stages add files AFTER
    #     the step-1 co-member fixpoint, so a file pulled there never had the cross-file-member / co-declarer
    #     closures applied to IT. The C++ case: 2c's displacement pulls a type's method-definer .cc, but its
    #     OWNER TYPE's .h (and any THIRD-file forward-declarer `class B;` of that type) was never pulled --
    #     the type isn't re-authored, so the read-back drops its out-of-line methods / DEFINED_IN edges
    #     (incremental != full, seen on Catch2: editing a matcher dropped SourceLineInfo/ITestInvoker methods).
    #     Re-run the SAME closures as a bounded monotonic fixpoint over the FINAL emit set -- co-member,
    #     co-member-owner, AND the displacement hook (which does C++'s owner + forward-declarer pull) -- so a
    #     type newly brought in by one closure gets its whole contributor set pulled by the next iteration,
    #     parsing each newly-pulled file. No-op for single-file-member languages with no displacement hook.
    if spec.definition_rels and (spec.type_member_rels or spec.member_owner_rels or spec.displaced_caller_files):
        _rm_set = set(removed_file_ids)
        while True:
            cur_emit_ids = {scope_id_for(proj_id, p) for p in all_emit_paths}
            seed = cur_emit_ids | _rm_set
            new = (_comember_file_ids(writer, spec, seed)
                   | _comember_owner_file_ids(writer, spec, seed)) - cur_emit_ids - _rm_set
            if spec.displaced_caller_files is not None:
                # the hook does C++'s co-declarer pull (forward-declarer headers of types the emit defines),
                # which only reaches a type once its definer .h is in the emit set -- hence inside the fixpoint.
                new |= {str(f) for f in spec.displaced_caller_files(writer, proj_id, [r for _, r in parsed])} \
                    - cur_emit_ids - _rm_set
            new_paths = [
                key_to_path[scope_id_to_key[f]] for f in new
                if f in scope_id_to_key and scope_id_to_key[f] in key_to_path
                and scope_id_to_key[f] not in changed_keys and scope_id_to_key[f] not in removed_keys
            ]
            new_paths = [p for p in dict.fromkeys(new_paths) if p not in all_emit_paths]
            if not new_paths:
                break
            with phase("parse"):
                parsed.extend((p, parse_file(source.read_blob(path_to_hash[p]), p)) for p in new_paths)
            all_emit_paths = all_emit_paths + new_paths

    broken_files = sorted(p for p, r in parsed if spec.is_broken(r))
    ok = [(p, r) for p, r in parsed if not spec.is_broken(r)]
    emit_paths = [p for p, _ in ok]
    emit_raws = [r for _, r in ok]

    # 3. Seed the resolution index from the graph for every OTHER file; emit the target subgraph.
    emit_keys = {scope_key_for(p) for p in emit_paths}
    ref_names: set[str] = set()
    for r in emit_raws:
        ref_names |= spec.referenced_names(r)
    with phase("index_seed"):
        index = spec.read_index(writer, proj_id, emit_keys | removed_keys, ref_names)
    with phase("resolve"):
        target_items: list = [spec.project_node(project_name, repo_uri, proj_id, target_sha, ref)]
        target_items.extend(spec.resolve_incremental(emit_raws, proj_id, index))
    target_node_ids = {str(n.key["id"]) for n in target_items if isinstance(n, GraphNode)}

    # 5a. Delete removed files' file-scoped subgraph (File + statics/macros). Their DEFINES/DECLARES
    #     edges vanish with the File node; the program-global nodes they authored survive here and
    #     are resolved by the orphan sweep (5c). Capture their extern refs first (DETACH drops them).
    removed_nodes = 0
    orphan_candidates: set[tuple[str, str]] = set()
    with phase("write"):
        writer.merge_nodes([unstamped(target_items[0])])  # no commit claimed from the first mutation on
        for fid in removed_file_ids:
            orphan_candidates |= _externs_referenced_by(writer, spec, fid)
            removed_nodes += _delete_subgraph(writer, spec, fid)
        # A `type_impl_rels` edge (Rust IMPLEMENTS) is sourced at a TYPE node that often lives in a
        # DIFFERENT, surviving file than the `impl` that authored it, and a methodless impl leaves no
        # node whose removal drops it -- so the DETACH DELETE above never reaches it. Delete these edges
        # by their `_src_file` provenance stamp, BEFORE the read-back, so a deleted impl file never
        # leaves a stale IMPLEMENTS edge (whether or not the sourced type is re-claimed this sync).
        _delete_authored_edges(writer, spec, removed_file_ids)

    # 5b. Provenance-scoped read-back + diff for the emit set. `touched_prog_ids` is passed in
    #     explicitly because on a RENAME the program-global node's OLD defining file was just
    #     deleted (5a) and its NEW file isn't ingested yet, so emit-file provenance can't reach it
    #     -- yet its stale lateral edges (a call it no longer makes) must still be diffed away.
    emit_file_ids = [scope_id_for(proj_id, p) for p in emit_paths]
    with phase("read_fingerprints"):
        cur_nodes, cur_edges = read_authored_fingerprints(writer, spec, emit_file_ids, touched_defined_ids)
    delta = compute_delta(cur_nodes, cur_edges, target_items)

    # 5c. Shared nodes that lost their LAST definer this sync (in touched_prog, not re-emitted; the
    #     co-contributor closure guarantees every surviving author was in the emit set, so absence
    #     from the target means genuinely un-defined). These program-global nodes are NOT under a
    #     file prefix, so removing them needs an explicit subtree delete (their children -- Params,
    #     Fields -- nest under the node id, and FalkorDB drops edges but not child nodes on delete).
    # Aggregation nodes are excluded here and reclaimed by refcount below: they aren't pulled (see the
    # closure), so their surviving siblings aren't in target_node_ids -- the naive "not in target" diff
    # would wrongly delete a still-authored shared parent (Java's `com`). Program-global symbols, which
    # ARE co-contributor-pulled, use the pull-diff as before.
    gone = [(nid, lbl) for nid, lbl in touched_defined
            if nid not in target_node_ids and lbl not in spec.aggregation_labels]
    touched_agg = [(nid, lbl) for nid, lbl in touched_defined if lbl in spec.aggregation_labels]

    # extern:// placeholders the dropped edges pointed at -> reclaim candidates (import direction).
    for _rel, _fl, _fk, to_label, to_key in delta.edges_to_remove:
        tid = str(to_key.get("id", ""))
        if tid.startswith("extern://") and to_label in spec.node_labels:
            orphan_candidates.add((tid, to_label))

    with phase("write"):
        from auryn.core.linkage import collect_linkages, demote_node, reconcile_linkages

        # Demote-before-delete: a linkable node being removed that still has cross-repo inbound refs
        # is re-materialized as its placeholder first (FalkorDB drops all edges on node delete). The
        # returned linkage feeds reconcile -- a THIRD repo may define the symbol, so the consumer
        # folds onto that real definition rather than the fresh placeholder.
        demoted = [
            lk for nid, lbl in gone if lbl in spec.linkable_labels
            for lk in [demote_node(writer, proj_id, lbl, nid, spec.child_sep)] if lk
        ]
        for nid, _lbl in gone:
            removed_nodes += _delete_node_subtree(writer, spec, nid)
        writer.apply_delta(delta)  # file-local removals, edge deltas, upserts (gone parents already gone -> no-op)
        reals, placeholders = collect_linkages(target_items)
        reconcile_linkages(writer, reals, placeholders + demoted)
        reclaimed = _reclaim_orphans(writer, orphan_candidates)
        # Refcount-GC aggregation nodes (Package/Module) a touched file used to define: now that the
        # delta is applied (surviving siblings' DEFINES edges are back; removed files' are gone), any
        # with zero remaining File-DEFINES edge is orphaned and reclaimed. O(touched), not O(project).
        removed_nodes += _reclaim_orphaned_aggregation(writer, spec, touched_agg)

    nodes_added = sum(1 for n in delta.nodes_to_upsert if node_key(n) not in cur_nodes)
    edges_added = sum(1 for e in delta.edges_to_upsert if edge_key(e) not in cur_edges)
    return ScopedSyncStats(
        nodes_added=nodes_added,
        nodes_updated=len(delta.nodes_to_upsert) - nodes_added,
        nodes_removed=len(delta.nodes_to_remove) + removed_nodes + reclaimed,
        edges_added=edges_added,
        edges_updated=len(delta.edges_to_upsert) - edges_added,
        edges_removed=len(delta.edges_to_remove),
        commit_sha=target_sha,
        elapsed_seconds=time.monotonic() - started,
        files_parsed=len(emit_raws),
        files_cached=len(path_to_hash) - len(emit_raws),
        broken_files=broken_files,
    )


def _defined_nodes(writer: GraphWriter, spec: ScopedSpec, file_ids: list[str], rels=None) -> list[tuple[str, str]]:
    """(id, semantic_label) of every program-global node these files reach via `rels` (default: all
    provenance_rels = DEFINES|DECLARES; pass definition_rels for DEFINES-only) -- the provenance-edge
    analogue of `_symbols_under` for nodes not nested under a file id."""
    rels = rels or spec.provenance_rels
    if not rels or not file_ids:
        return []
    rel = "|".join(rels)
    rows = writer.ro_query(
        f"MATCH (f:File)-[:{rel}]->(n) WHERE f.id IN $fids RETURN DISTINCT n.id, {semantic_label('n')}",
        {"fids": list(file_ids)},
    ).result_set
    return [(str(nid), str(lbl)) for nid, lbl in rows]


def _cocontributor_file_ids(writer: GraphWriter, spec: ScopedSpec, node_ids: list[str]) -> set[str]:
    """File ids of every file that DEFINES/DECLARES any of these shared nodes -- so a change to one
    author of a shared node re-emits ALL its authors (props re-reduce; the node survives iff still
    defined). `(n:Entity) WHERE n.id IN [...]` is an id-index seek (Entity carries the id index)."""
    if not spec.provenance_rels or not node_ids:
        return set()
    rel = "|".join(spec.provenance_rels)
    rows = writer.ro_query(
        f"MATCH (f:File)-[:{rel}]->(n:{SHARED_NODE_LABEL}) WHERE n.id IN $nids RETURN DISTINCT f.id",
        {"nids": list(node_ids)},
    ).result_set
    return {str(r[0]) for r in rows}


def _codefiner_file_ids(writer: GraphWriter, spec: ScopedSpec, file_ids) -> set[str]:
    """File ids OUTSIDE `file_ids` that DEFINE a symbol an emit file also DEFINES -- the co-definers
    of a multiply-defined symbol (multi-program dirs: many `main`, several `struct config`). Pulling
    them in lets the emit set reproduce the same order-independent winner + merged edges a full
    recompute produces (ADR 0005 #2). `ef.id IN [...]` seeds the File id index; a no-op when nothing
    is multiply-defined (the common case)."""
    if not spec.definition_rels or not file_ids:
        return set()
    defn = "|".join(spec.definition_rels)
    # Skip aggregation nodes (Module/Package): id-derived props, so no winner to reconcile -- and one
    # (Rust's collapsed `mod tests`) can be co-authored by dozens of files, which would cascade the
    # whole crate in. `labels(s)` is a cheap post-filter; the seek is still `ef.id IN $ids` (File idx).
    agg_filter = " AND NONE(l IN labels(s) WHERE l IN $agg)" if spec.aggregation_labels else ""
    params = {"ids": list(file_ids)}
    if spec.aggregation_labels:
        params["agg"] = list(spec.aggregation_labels)
    rows = writer.ro_query(
        f"MATCH (ef:File)-[:{defn}]->(s)<-[:{defn}]-(of:File) "
        f"WHERE ef.id IN $ids AND NOT of.id IN $ids{agg_filter} RETURN DISTINCT of.id",
        params,
    ).result_set
    return {str(r[0]) for r in rows}


def _comember_file_ids(writer: GraphWriter, spec: ScopedSpec, file_ids) -> set[str]:
    """File ids OUTSIDE `file_ids` that DEFINE a MEMBER (via `spec.type_member_rels`, e.g. HAS_METHOD)
    of a TYPE an emit file DEFINES -- i.e. the files holding a touched type's `impl` blocks / sibling-
    file methods. Pulling them re-authors the type's whole subgraph (cross-file methods + IMPLEMENTS/
    EXTENDS edges), so the provenance-scoped read-back never claims a cross-file member/edge the emit
    set can't reproduce. A no-op when `type_member_rels` is empty (single-file-member languages). Only
    the type (`t`) must be emit-defined; the member rel is type->member so `t` is never an aggregation
    node, so no crate-wide cascade (unlike module containment)."""
    if not spec.type_member_rels or not spec.definition_rels or not file_ids:
        return set()
    defn = "|".join(spec.definition_rels)
    mem = "|".join(spec.type_member_rels)
    rows = writer.ro_query(
        f"MATCH (ef:File)-[:{defn}]->(t)-[:{mem}]->(member)<-[:{defn}]-(mf:File) "
        f"WHERE ef.id IN $ids AND NOT mf.id IN $ids RETURN DISTINCT mf.id",
        {"ids": list(file_ids)},
    ).result_set
    return {str(r[0]) for r in rows}


def _comember_owner_file_ids(writer: GraphWriter, spec: ScopedSpec, file_ids) -> set[str]:
    """File ids OUTSIDE `file_ids` that DEFINE the OWNER TYPE of a member an emit file DEFINES -- the
    REVERSE of `_comember_file_ids` (see ScopedSpec.member_owner_rels). C++ out-of-line method defs live
    in a .cc apart from the class body in a .h; editing the .cc must pull the .h so the type's whole
    subgraph (its other methods/fields + the merged in-class declaration of this method) is re-authored
    together -- otherwise the provenance read-back claims them and compute_delta drops them. A no-op when
    `member_owner_rels` is empty (single-file-member languages, where the owner's file IS the member's)."""
    if not spec.member_owner_rels or not spec.definition_rels or not file_ids:
        return set()
    defn = "|".join(spec.definition_rels)
    mem = "|".join(spec.member_owner_rels)
    rows = writer.ro_query(
        f"MATCH (ef:File)-[:{defn}]->(member)<-[:{mem}]-(t)<-[:{defn}]-(tf:File) "
        f"WHERE ef.id IN $ids AND NOT tf.id IN $ids RETURN DISTINCT tf.id",
        {"ids": list(file_ids)},
    ).result_set
    return {str(r[0]) for r in rows}


def _delete_authored_edges(writer: GraphWriter, spec: ScopedSpec, removed_file_ids) -> None:
    """Delete `type_impl_rels` edges (Rust IMPLEMENTS) authored by a REMOVED file, found by their
    `_src_file` provenance stamp. The edge is source-owned at a TYPE node that may live in another,
    surviving file, so the removed file's DETACH DELETE never touches it -- and a methodless impl leaves
    no node whose removal would. A no-op unless the language stamps impl provenance (type_impl_rels);
    legacy edges (no `_src_file`, from a pre-provenance graph) aren't matched and heal on a full ingest."""
    if not spec.type_impl_rels or not removed_file_ids:
        return
    rels = "|".join(spec.type_impl_rels)
    writer.query(
        f"MATCH ()-[r:{rels}]->() WHERE r._src_file IN $fids DELETE r",
        {"fids": list(removed_file_ids)},
    )


def _caller_files(writer: GraphWriter, spec: ScopedSpec, symbols_by_label: dict[str, list[str]]) -> set[str]:
    """File ids of every file whose entities reference (symbol_ref_rels) any of these symbols. A
    caller node is program-global (not under a file prefix), so it is mapped to its DEFINING FILE
    via provenance. `sym:{label} WHERE sym.id IN [...]` seeks the per-label id index."""
    if not spec.provenance_rels:
        return set()
    rel = "|".join(spec.symbol_ref_rels)
    prov = "|".join(spec.provenance_rels)
    out: set[str] = set()
    for label, sids in symbols_by_label.items():
        if not sids:
            continue
        rows = writer.ro_query(
            f"MATCH (x)-[:{rel}]->(sym:{label}) WHERE sym.id IN $ids "
            f"MATCH (fr:File)-[:{prov}]->(x) RETURN DISTINCT fr.id",
            {"ids": sids},
        ).result_set
        out |= {str(r[0]) for r in rows}
    return out


def _placeholder_caller_files(writer: GraphWriter, spec: ScopedSpec, satisfied_keys: set[str]) -> set[str]:
    """File ids of files that reference an `extern://` placeholder whose linkage a just-parsed emit
    file now satisfies (bare-name resolution has no dependency edge for the standard closures).
    `p.linkage_key IN [...]` seeks the linkage_key index on the linkable label."""
    if not satisfied_keys or not spec.provenance_rels:
        return set()
    rel = "|".join(spec.symbol_ref_rels)
    prov = "|".join(spec.provenance_rels)
    ks = list(satisfied_keys)
    out: set[str] = set()
    for label in spec.linkable_labels:  # placeholders wear a linkable label (linkage_key indexed)
        rows = writer.ro_query(
            f"MATCH (x)-[:{rel}]->(p:{label}) WHERE p.linkage_key IN $ks AND p.resolved = false "
            f"MATCH (fr:File)-[:{prov}]->(x) RETURN DISTINCT fr.id",
            {"ks": ks},
        ).result_set
        out |= {str(r[0]) for r in rows}
    return out


def _foreign_defined(writer: GraphWriter, defn_rels, node_ids: list[str],
                     emit_file_ids: set[str]) -> set[str]:
    """Of `node_ids`, those DEFINED by at least one file but by NONE in the emit set -- i.e. owned by
    a file OUTSIDE the emit set. The case this guards: a trait-impl method lives in the impl's file,
    which may differ from the struct's file (`impl Trait for Struct` in another file/crate, or a Go
    method on a type declared elsewhere). When only the struct's file is in the emit set, the method
    is read as a struct child in (c) but the emit set doesn't re-author it -- so compute_delta would
    delete a method whose (unchanged) impl file wasn't re-emitted. A pure child (Parameter/Field: no
    DEFINES edge of its own) has no definer row and is never foreign."""
    if not node_ids or not defn_rels:
        return set()
    defn_pat = "|".join(defn_rels)
    foreign: set[str] = set()
    for cid, files in writer.ro_query(
        f"MATCH (f:File)-[:{defn_pat}]->(c) WHERE c.id IN $ids RETURN c.id, collect(DISTINCT f.id)",
        {"ids": node_ids},
    ).result_set:
        if files and not (set(files) & emit_file_ids):
            foreign.add(str(cid))
    return foreign


def read_authored_fingerprints(writer: GraphWriter, spec: ScopedSpec, emit_file_ids: list[str],
                               extra_node_ids: list[str] = ()):
    """(node_fps, edge_fps) for an authored-shared emit set. A file 'owns': its File node +
    file-prefixed nodes (statics/macros), the program-global nodes it DEFINES/DECLARES, and those
    nodes' children. Edges: source-owned lateral + provenance (File→entity), target-owned
    containment. Because the emit set is co-contributor-closed, every shared node read here is
    FULLY re-authored by the emit set, so compute_delta diffs it exactly. `extra_node_ids` are
    program-global nodes touched by the change whose defining file may have just been deleted (a
    rename), read directly by id so their stale edges are still diffed."""
    node_fps: dict = {}
    edge_fps: dict = {}
    owned_ids: set[str] = set()
    prog_ids: list[str] = []
    lat, con, prov = list(spec.lateral_rels), list(spec.containment_rels), list(spec.provenance_rels)
    # A node's DEFINITION data (props, lateral edges, children) is owned by its DEFINES file, not a
    # header that merely DECLARES it -- so ownership uses definition_rels, while the provenance EDGE
    # read below still covers DECLARES (a header owns its own declares edge). Absent split, both = prov.
    defn = list(spec.definition_rels) or prov

    # (a) file-prefixed nodes: File, statics, macros (id == file_id or nests under it). The two-sided
    # prefix range over-matches a SIBLING file whose id merely shares a string prefix (``foo.c`` also
    # catching ``foo.cpp``/``foo.cc``, ``a.h`` catching ``a.hpp``), so keep only ids this file truly
    # owns -- itself or a child-separated descendant (``foo.c.func.bar``). Without this, resyncing
    # ``foo.c`` reads ``foo.cpp``'s File/statics as "current", and compute_delta deletes them
    # (incremental != full-recompute data loss, the authored-engine twin of the Tier-0 read fix).
    sep = spec.child_sep
    for fid in emit_file_ids:
        lo, hi = _range(fid)
        for label in spec.node_labels:
            for nid, fp in writer.ro_query(
                f"MATCH (n:{label}) WHERE n.id >= $lo AND n.id < $hi RETURN n.id, n._fingerprint",
                {"lo": lo, "hi": hi},
            ).result_set:
                if nid != fid and not str(nid).startswith(fid + sep):
                    continue  # a prefix-sibling file, not owned by this one
                node_fps[node_key(GraphNode(label, {"id": nid}, {}))] = fp
                owned_ids.add(str(nid))

    # (b) program-global nodes DEFINED by the emit files (definition owns props/lateral/children;
    #     a symbol merely DECLARED by an emit header is NOT read here -- its data lives with its def).
    if defn and emit_file_ids:
        defn_pat = "|".join(defn)
        for nid, lbl, fp in writer.ro_query(
            f"MATCH (f:File)-[:{defn_pat}]->(n) WHERE f.id IN $fids "
            f"RETURN DISTINCT n.id, {semantic_label('n')}, n._fingerprint",
            {"fids": list(emit_file_ids)},
        ).result_set:
            # Aggregation nodes (Package/Module) that an emit file DEFINES are NOT owned here: their
            # props are id-derived (never diffed) and their containment children span the WHOLE
            # package/module (every co-authoring file). Treating one as owned would pull all those
            # children into (c) and make compute_delta delete the siblings the partial emit set didn't
            # re-emit. Their own node/HAS_* edge is harmlessly re-upserted from the target; their GC is
            # the refcount sweep (_reclaim_orphaned_aggregation). The children are owned by THEIR files.
            if str(lbl) in spec.aggregation_labels:
                continue
            node_fps[node_key(GraphNode(str(lbl), {"id": nid}, {}))] = fp
            owned_ids.add(str(nid))
            prog_ids.append(str(nid))

    # (b2) touched program-global nodes named directly by id (a renamed file's defs, whose old
    #      provenance edge was already deleted -> unreachable via (b) but still carrying stale edges).
    if extra_node_ids:
        for nid, lbl, fp in writer.ro_query(
            f"MATCH (n:{SHARED_NODE_LABEL}) WHERE n.id IN $ids "
            f"RETURN n.id, {semantic_label('n')}, n._fingerprint",
            {"ids": list(extra_node_ids)},
        ).result_set:
            if str(lbl) in spec.aggregation_labels:
                continue  # same reason as (b): an aggregation node must not own its package's children
            node_fps[node_key(GraphNode(str(lbl), {"id": nid}, {}))] = fp
            owned_ids.add(str(nid))
            prog_ids.append(str(nid))

    # (c) children (containment) of those program-global nodes (Parameters/Fields/Variants...), BUT
    # not a child that is itself DEFINED only by a file outside the emit set -- a trait-impl method
    # whose impl lives in a non-emit file is owned by THAT file, not by this parent's struct's file
    # (see _foreign_defined). Reading it would make compute_delta delete it though the emit set never
    # re-authors it (the cross-file-impl data loss; incremental != full). The foreign check only
    # applies -- and only runs its extra query -- when a type's members CAN live in another file
    # (`type_member_rels`, i.e. Rust/Go); for single-file-member languages (C struct fields, Java
    # class methods) every child is owned by its parent's file, so it is skipped (it would be a no-op
    # that an extra `DEFINES`-collect query over a large graph can make expensive -- it timed out on
    # git's ~58k-node C graph, silently stranding the C resync).
    if con and prog_ids:
        con_pat = "|".join(con)
        child_rows = writer.ro_query(
            f"MATCH (p:{SHARED_NODE_LABEL})-[:{con_pat}]->(child) WHERE p.id IN $pids "
            f"RETURN DISTINCT child.id, {semantic_label('child')}, child._fingerprint",
            {"pids": prog_ids},
        ).result_set
        foreign = (_foreign_defined(writer, defn, [str(c) for c, _, _ in child_rows], set(emit_file_ids))
                   if spec.type_member_rels else frozenset())
        for cid, lbl, fp in child_rows:
            if str(cid) in foreign:
                continue
            node_fps[node_key(GraphNode(str(lbl), {"id": cid}, {}))] = fp
            owned_ids.add(str(cid))

    ids_list = list(owned_ids)
    # lateral edges (source owned): CALLS/REFERENCES/INCLUDES. `a:Entity ... a.id IN` -> id index.
    if lat and ids_list:
        impl_rels = set(spec.type_impl_rels)
        emit_set = set(emit_file_ids)
        for rel, fl, fid, tl, tid, fp, src in writer.ro_query(
            f"MATCH (a:{SHARED_NODE_LABEL})-[r]->(b) WHERE a.id IN $ids AND type(r) IN $lat "
            f"RETURN type(r), {semantic_label('a')}, a.id, {semantic_label('b')}, b.id, r._fingerprint, r._src_file",
            {"ids": ids_list, "lat": lat},
        ).result_set:
            # A `type_impl_rels` edge (Rust IMPLEMENTS) may be authored by a file OTHER than its source
            # type's -- `impl Trait for Type` can sit in the trait's file or any file in the crate (the
            # orphan rule), and a methodless impl leaves no node to pull that file in. Each such edge
            # carries `_src_file` (its authoring file, stamped by the ingestor); if that file is a
            # SURVIVING non-emit file the emit set won't reproduce the edge, so exclude it from the
            # read-back and leave it in place -- otherwise compute_delta deletes an IMPLEMENTS edge no one
            # re-authored (the edge-level analogue of the `_foreign_defined` node filter). An edge whose
            # `_src_file` is IN the emit set (its own file edited) is diffed normally; one whose `_src_file`
            # is a REMOVED file was already deleted up front by `_delete_authored_edges` (5a), so it won't
            # be read here. Residuals (narrow, documented; the robust fix is modeling impls as nodes):
            # (1) an unstamped edge from a PRE-provenance graph has src=None -> treated as foreign/kept
            #     (never a data loss); a removal of such an impl heals only on the next full ingest.
            # (2) generic sibling impls (`impl T<u8> for S` / `impl T<u16> for S` in different files)
            #     collapse to one edge (the parser strips generics, edge_key ignores props), so its single
            #     `_src_file` names just one author -- harmless to the edge's existence, but its provenance
            #     is ambiguous if those authors are split across emit/non-emit files.
            if rel in impl_rels and src not in emit_set:
                continue
            edge_fps[edge_key(GraphEdge(rel, fl, {"id": fid}, tl, {"id": tid}, {}))] = fp
    # provenance edges (source = a File in the emit set): DEFINES/DECLARES.
    if prov and emit_file_ids:
        for rel, fid, tl, tid, fp in writer.ro_query(
            f"MATCH (f:File)-[r]->(b) WHERE f.id IN $fids AND type(r) IN $prov "
            f"RETURN type(r), f.id, {semantic_label('b')}, b.id, r._fingerprint",
            {"fids": list(emit_file_ids), "prov": prov},
        ).result_set:
            edge_fps[edge_key(GraphEdge(rel, "File", {"id": fid}, tl, {"id": tid}, {}))] = fp
    # containment edges (target owned): HAS_FIELD/HAS_PARAMETER. `b:Entity ... b.id IN` -> id index.
    if con and ids_list:
        for rel, fl, fid, tl, tid, fp in writer.ro_query(
            f"MATCH (a)-[r]->(b:{SHARED_NODE_LABEL}) WHERE b.id IN $ids AND type(r) IN $con "
            f"RETURN type(r), {semantic_label('a')}, a.id, {semantic_label('b')}, b.id, r._fingerprint",
            {"ids": ids_list, "con": con},
        ).result_set:
            edge_fps[edge_key(GraphEdge(rel, fl, {"id": fid}, tl, {"id": tid}, {}))] = fp
    return node_fps, edge_fps


# ── closure ──────────────────────────────────────────────────────────────────────────
def _owning_key(node_id: str, scope_id_to_key: dict[str, str], sep: str) -> str | None:
    """The scope key that owns `node_id`: the longest scope id that is a prefix of it (the
    node id equals the scope id, or continues it with the child separator). Robust to scope
    keys that contain the separator (nested packages, dotted filenames) -- only real scope ids
    are candidates, and the longest wins."""
    best: str | None = None
    for sid in scope_id_to_key:
        if node_id == sid or node_id.startswith(sid + sep):
            if best is None or len(sid) > len(best):
                best = sid
    return scope_id_to_key[best] if best is not None else None


def _symbols_under(writer: GraphWriter, spec: ScopedSpec, scope_ids: list[str]) -> dict[str, list[str]]:
    """The ids of every lateral-targetable symbol under these scopes, keyed BY LABEL so the
    reverse-dep query can seek `(sym:Label)` by id via the range index instead of scanning
    every node (confirmed via GRAPH.EXPLAIN)."""
    by_label: dict[str, list[str]] = {}
    for scope_id in scope_ids:
        lo, hi = _range(scope_id)
        for label in spec.targetable_labels:
            rows = writer.ro_query(
                f"MATCH (n:{label}) WHERE n.id >= $lo AND n.id < $hi RETURN n.id", {"lo": lo, "hi": hi}
            ).result_set
            if rows:
                by_label.setdefault(label, []).extend(r[0] for r in rows)
    return by_label


def _affected_by_symbol_ref(writer, spec, symbols_by_label, scope_id_to_key) -> set[str]:
    rel_pattern = "|".join(spec.symbol_ref_rels)
    keys: set[str] = set()
    for label, ids in symbols_by_label.items():
        rows = writer.ro_query(
            f"MATCH (x)-[:{rel_pattern}]->(sym:{label}) WHERE sym.id IN $ids RETURN DISTINCT x.id", {"ids": ids}
        ).result_set
        keys |= _keys_of(rows, scope_id_to_key, spec.child_sep)
    return keys


def _affected_by_import(writer, spec, module_ids, scope_id_to_key) -> set[str]:
    if not spec.import_rel or not module_ids:
        return set()
    keys: set[str] = set()
    for label in spec.import_target_labels:  # labeled seek -> range index (not an all-node scan)
        rows = writer.ro_query(
            f"MATCH (x)-[:{spec.import_rel}]->(m:{label}) WHERE m.id IN $ids RETURN DISTINCT x.id", {"ids": module_ids}
        ).result_set
        keys |= _keys_of(rows, scope_id_to_key, spec.child_sep)
    return keys


def _affected_by_placeholder_callers(writer, spec, satisfied_keys, scope_id_to_key) -> set[str]:
    """Scope keys of files that reference an `extern://` placeholder whose linkage a just-parsed
    emit file now satisfies (see ScopedSpec.satisfied_linkage_keys). `linkage_key` is indexed on
    every targetable label, so `linkage_key IN [...] AND resolved = false` is an index seek over the
    bounded satisfied set -- no scan. No-op when nothing is satisfied."""
    if not satisfied_keys:
        return set()
    rel_pattern = "|".join(spec.symbol_ref_rels)
    ks = list(satisfied_keys)
    keys: set[str] = set()
    for label in spec.targetable_labels:  # placeholders wear a targetable label (e.g. Function)
        rows = writer.ro_query(
            f"MATCH (x)-[:{rel_pattern}]->(p:{label}) "
            "WHERE p.linkage_key IN $ks AND p.resolved = false RETURN DISTINCT x.id",
            {"ks": ks},
        ).result_set
        keys |= _keys_of(rows, scope_id_to_key, spec.child_sep)
    return keys


def _keys_of(rows, scope_id_to_key, sep) -> set[str]:
    keys: set[str] = set()
    for (xid,) in rows:
        k = _owning_key(xid, scope_id_to_key, sep)
        if k is not None:
            keys.add(k)
    return keys


# ── scoped read-back / delete ──────────────────────────────────────────────────────────
def read_scoped_fingerprints(writer: GraphWriter, spec: ScopedSpec, scope_ids: list[str],
                             all_scope_ids: frozenset[str] | None = None):
    """(node_fps, edge_fps) for exactly the emit scope: nodes under a scope, lateral edges
    whose SOURCE is under a scope, containment edges whose TARGET is under a scope -- mirroring
    which file re-emits each edge, so compute_delta never deletes an edge whose owner we
    didn't re-emit.

    "Under a scope" is child-separator ownership (the scope's own node, or a descendant keyed
    ``scope_id + child_sep + …``), NOT a raw string-prefix range -- otherwise a scope would swallow
    a sibling that merely shares a prefix (``…repocard`` vs ``…repocard_data``) or a nested
    sub-package, and compute_delta would then delete those un-re-emitted nodes (an
    incremental != full-recompute data-loss bug). `all_scope_ids` (every scope in the project) lets
    the longest-prefix filter exclude nodes a DEEPER scope owns; without it only prefix-siblings are
    excluded (still correct for flat module scopes, the common case)."""
    node_fps: dict = {}
    edge_fps: dict = {}
    lat = list(spec.lateral_rels)
    con = list(spec.containment_rels)
    sep = spec.child_sep
    scopes = all_scope_ids if all_scope_ids is not None else frozenset(scope_ids)
    for scope_id in scope_ids:
        # Read a superset with the ORIGINAL two-sided prefix range (proven on every FalkorDB build;
        # a narrower upper bound can trip an engine-specific range-scan miscount when a node sits at
        # the lower bound), then keep only the rows this scope actually OWNS via `_owns` -- excluding
        # a prefix-sibling (`…repocard` vs `…repocard_data`) and any nested sub-scope. The filter,
        # not the range width, is what prevents the incremental!=full data-loss.
        lo, hi = _range(scope_id)
        params = {"lo": lo, "hi": hi}
        owned = lambda nid: _owns(nid, scope_id, scopes, sep)  # noqa: E731
        for label in spec.node_labels:
            for nid, fp in writer.ro_query(
                f"MATCH (n:{label}) WHERE n.id >= $lo AND n.id < $hi RETURN n.id, n._fingerprint",
                params,
            ).result_set:
                if owned(nid):
                    node_fps[node_key(GraphNode(label, {"id": nid}, {}))] = fp
            if lat:
                for rel, to_label, from_id, to_id, fp in writer.ro_query(
                    f"MATCH (a:{label})-[r]->(b) WHERE a.id >= $lo AND a.id < $hi "
                    f"AND type(r) IN $lat RETURN type(r), {semantic_label('b')}, a.id, b.id, r._fingerprint",
                    {**params, "lat": lat},
                ).result_set:
                    if owned(from_id):
                        edge_fps[edge_key(GraphEdge(rel, label, {"id": from_id}, to_label, {"id": to_id}, {}))] = fp
            if con:
                for rel, from_label, from_id, to_id, fp in writer.ro_query(
                    f"MATCH (a)-[r]->(b:{label}) WHERE b.id >= $lo AND b.id < $hi "
                    f"AND type(r) IN $con RETURN type(r), {semantic_label('a')}, a.id, b.id, r._fingerprint",
                    {**params, "con": con},
                ).result_set:
                    if owned(to_id):
                        edge_fps[edge_key(GraphEdge(rel, from_label, {"id": from_id}, label, {"id": to_id}, {}))] = fp
    return node_fps, edge_fps


def _delete_subgraph(writer: GraphWriter, spec: ScopedSpec, scope_id: str) -> int:
    """DETACH DELETE the node at `scope_id` and every node keyed as a child-separated descendant of
    it (``scope_id`` itself, or ``scope_id + child_sep + …``). ``STARTS WITH scope_id`` is the proven,
    engine-portable prefix read (a two-sided ``[scope_id, scope_id+￿)`` range can trip a FalkorDB
    range-scan miscount when a node sits exactly at the lower bound), but it OVER-matches a SIBLING
    scope whose name merely shares a string prefix (``…module.repocard`` also catching
    ``…module.repocard_data``) -- so filter the ids to the ones this scope actually owns before
    deleting, else a removed/renamed file silently deletes a prefix-sibling's subgraph (an
    incremental != full-recompute data loss)."""
    sep = spec.child_sep
    removed = 0
    for label in spec.node_labels:
        ids = [
            r[0] for r in writer.ro_query(
                f"MATCH (n:{label}) WHERE n.id STARTS WITH $p RETURN n.id", {"p": scope_id}
            ).result_set
        ]
        owned = [i for i in ids if i == scope_id or i.startswith(scope_id + sep)]
        if owned:
            writer.query(
                f"MATCH (n:{label}) WHERE n.id IN $ids DETACH DELETE n", {"ids": owned}
            )
            removed += len(owned)
    return removed


def _delete_node_subtree(writer: GraphWriter, spec: ScopedSpec, node_id: str) -> int:
    """DETACH DELETE a program-global node AND its nested children (Parameters/Fields), which key
    under ``node_id + child_sep``. The node itself is a point delete; children are a range delete
    bounded by the child separator, so a sibling that merely shares a name prefix (``Point`` vs
    ``PointOther``) is never swept. FalkorDB drops the node's edges on delete."""
    clo, chi = node_id + spec.child_sep, node_id + spec.child_sep + chr(0xFFFF)
    for label in spec.node_labels:
        writer.query(
            f"MATCH (n:{label}) WHERE n.id >= $lo AND n.id < $hi DETACH DELETE n", {"lo": clo, "hi": chi}
        )
    writer.query(f"MATCH (n:{SHARED_NODE_LABEL} {{id: $id}}) DETACH DELETE n", {"id": node_id})
    return 1


def _reclaim_orphaned_aggregation(writer: GraphWriter, spec: ScopedSpec, agg_nodes) -> int:
    """Delete aggregation nodes (Package/Module -- `spec.aggregation_labels`) that lost their LAST
    File-[:DEFINES]-> edge this sync. Called AFTER the delta is applied, so the graph already reflects
    reality: a removed file's DEFINES edges are gone (its subgraph was DETACH-deleted) and a surviving
    file's are (still) present. Refcounting the remaining definers -- rather than pulling every
    co-definer to re-emit-and-diff, as program-global symbols do -- is what keeps deleting one file in
    a huge shared package (Java's `com`) O(change), not O(project). Correct for a leaf package AND an
    intermediate prefix: a prefix with no direct file is authored only via its descendants' chains, so
    when the last descendant goes its refcount hits zero too. The `n.id IN $ids` seek uses the id index."""
    if not agg_nodes:
        return 0
    rel = "|".join(spec.definition_rels or spec.provenance_rels)
    by_label: dict[str, list[str]] = {}
    for nid, lbl in agg_nodes:
        by_label.setdefault(lbl, []).append(nid)
    removed = 0
    for label, ids_ in by_label.items():
        rows = writer.ro_query(
            f"MATCH (n:{label}) WHERE n.id IN $ids "
            f"OPTIONAL MATCH (f:File)-[:{rel}]->(n) "
            "WITH n.id AS nid, count(f) AS defs WHERE defs = 0 RETURN nid",
            {"ids": ids_},
        ).result_set
        for (nid,) in rows:
            # Point-delete by EXACT id -- NOT _delete_node_subtree. An aggregation node's real
            # children (its packages/types) are reclaimed independently this sync (types via the
            # `gone` sweep, sub-aggregations via their own refcount), so a subtree delete is
            # unnecessary here. Worse, _delete_node_subtree's `.`-child range [id., id.￿) would
            # sweep a dot-prefixed SIBLING: reclaiming Module `api` would delete the still-live
            # `api.v2` (its id starts with `api.`). Unlike the package prefix chain -- where a parent
            # keeps a File-DEFINES refcount while any descendant has files -- sibling modules are
            # independent, so `api` can orphan while `api.v2` lives.
            writer.query(f"MATCH (n:{SHARED_NODE_LABEL} {{id: $id}}) DETACH DELETE n", {"id": str(nid)})
            removed += 1
    return removed


def _externs_referenced_by(writer: GraphWriter, spec: ScopedSpec, scope_id: str) -> set[tuple[str, str]]:
    """(extern id, label) of every ``extern://`` placeholder a scope points at via a lateral edge.
    Collected before a removed scope is DETACH-deleted, so a placeholder the deleted file was the
    last caller of can still be reclaimed. Labeled range seek per node label -- no scan."""
    if not spec.lateral_rels:
        return set()
    lo, hi = _range(scope_id)
    lat = list(spec.lateral_rels)
    out: set[tuple[str, str]] = set()
    for label in spec.node_labels:
        for eid, elabel in writer.ro_query(
            f"MATCH (a:{label})-[r]->(e) WHERE a.id >= $lo AND a.id < $hi AND type(r) IN $lat "
            f"AND e.id STARTS WITH 'extern://' RETURN DISTINCT e.id, {semantic_label('e')}",
            {"lo": lo, "hi": hi, "lat": lat},
        ).result_set:
            out.add((str(eid), elabel))
    return out


def _reclaim_orphans(writer: GraphWriter, candidates: set[tuple[str, str]]) -> int:
    """Delete cross-repo placeholder nodes (``extern://…``) that no edge anywhere still references.
    A full recompute only emits a placeholder while something refers to it, so without this an
    incremental resync would keep a stray placeholder a full recompute drops (breaking the
    incremental==full oracle -- acutely so for command-heavy languages like Bash, where every
    external command is a placeholder). A placeholder lives OUTSIDE every file scope, so the scoped
    read/diff never sees the node itself; `candidates` are the placeholders whose last inbound edge
    this resync may have removed (from the delta) or DETACH-deleted (from a removed file).

    Each check/delete is an id-indexed seek over the bounded candidate set -- no scan, and a no-op
    unless a reference was genuinely the last. reconcile_linkages runs first, so a placeholder
    already folded into a real definer is gone and simply isn't found here."""
    reclaimed = 0
    for eid, label in candidates:
        # count(r) reflects the post-write state (the removed/detached edge is already gone). 0 rows
        # -> the node no longer exists (absorbed by reconcile); one row with count 0 -> a genuine
        # orphan to delete; count>0 -> still referenced elsewhere, keep.
        rows = writer.ro_query(
            f"MATCH (n:{label}) WHERE n.id = $id OPTIONAL MATCH ()-[r]->(n) RETURN count(r)",
            {"id": eid},
        ).result_set
        if rows and rows[0][0] == 0:
            writer.query(f"MATCH (n:{label}) WHERE n.id = $id DETACH DELETE n", {"id": eid})
            reclaimed += 1
    return reclaimed


# ── git routing (shared by every scoped-language git_sync) ─────────────────────────────
def route_incremental(writer, spec, source, target_sha, ref, current_sha, *, project_name, repo_uri, logger):
    """Attempt an incremental resync from whichever git producer the source offers; return
    ScopedSyncStats on success or None to fall back to the full recompute (which leaves the
    graph untouched). Two producers: a working tree (file/project save, diff vs base + dirty
    set) or a bare clone (commit/checkout, commit-to-commit diff)."""
    from auryn.core.local_source import WorkingTreeSource

    proj_id = spec.project_id(project_name)
    suffix_for_diff = spec.file_suffixes  # a language may own several suffixes
    # Authored-shared languages (ADR 0005) take the provenance-scoped engine; everyone else the
    # id-prefix engine. Same closure + git producers; only the read/delete/refcount differ.
    engine = incremental_sync_authored if spec.provenance_rels else incremental_sync

    if isinstance(source, WorkingTreeSource):
        base = writer.get_property("Project", {"id": proj_id}, "base_commit")
        if not base:
            return None
        try:
            raw = _multi_suffix_diff(source.changed_since, base, suffix_for_diff)
        except Exception as e:
            logger.info("Working-tree incremental unavailable (%s); full recompute", e)
            return None
        changes = _worktree_changeset(source, raw, _prev_dirty(writer, proj_id))
        logger.info("Incremental resync (%s working tree, base %s): %d changed, %d deleted",
                    spec.language, base[:12], len(changes.modified), len(changes.deleted))
        return engine(writer, spec, source, target_sha, changes,
                      project_name=project_name, repo_uri=repo_uri, ref=ref)

    if not hasattr(source, "diff_changed_files"):
        return None
    try:
        raw = _multi_suffix_diff(lambda s, suf: source.diff_changed_files(current_sha, target_sha, suf),
                                 None, suffix_for_diff)
    except Exception as e:
        logger.info("Incremental resync unavailable (%s); falling back to full recompute", e)
        return None
    changes = ChangeSet(modified=raw["modified"], added=raw["added"], deleted=raw["deleted"], renamed=raw["renamed"])
    logger.info("Incremental resync (%s) from %s: %d modified, %d added, %d deleted, %d renamed",
                spec.language, current_sha[:12], len(changes.modified), len(changes.added),
                len(changes.deleted), len(changes.renamed))
    return engine(writer, spec, source, target_sha, changes,
                  project_name=project_name, repo_uri=repo_uri, ref=ref)


def _multi_suffix_diff(diff_fn, base, suffixes) -> dict:
    """Run a per-suffix diff producer once per suffix and merge, so a language owning several
    extensions (e.g. .ts/.tsx/.js) gets one changed set."""
    out = {"modified": [], "added": [], "deleted": [], "renamed": []}
    for suffix in suffixes:
        part = diff_fn(base, suffix) if base is not None else diff_fn(None, suffix)
        for k in ("modified", "added", "deleted", "renamed"):
            out[k].extend(part[k])
    return out


def _prev_dirty(writer: GraphWriter, proj_id: str) -> set[str]:
    raw = writer.get_property("Project", {"id": proj_id}, "dirty_paths")
    if not raw:
        return set()
    try:
        return set(json.loads(raw))
    except Exception:
        return set()


def _worktree_changeset(source, raw: dict, prev_dirty: set[str]) -> ChangeSet:
    root = Path(source._root)
    candidates = set(raw["modified"]) | set(raw["added"]) | set(raw["deleted"]) | set(prev_dirty)
    for old, new in raw["renamed"]:
        candidates.update((old, new))
    modified = sorted(p for p in candidates if (root / p).exists())
    deleted = sorted(p for p in candidates if not (root / p).exists())
    return ChangeSet(modified=modified, deleted=deleted)


def stamp_worktree(writer: GraphWriter, proj_id: str, source, suffixes: tuple[str, ...]) -> None:
    """Store base_commit (= current HEAD) and dirty_paths (files uncommitted now) on the
    Project node, so the next save-triggered resync can diff against them (dirty set makes a
    revert-to-committed re-emit)."""
    head = source.head()
    paths: set[str] = set()
    for suffix in suffixes:
        d = source.changed_since(head, suffix)
        paths |= set(d["modified"]) | set(d["added"]) | set(d["deleted"])
        paths |= {p for pair in d["renamed"] for p in pair}
    writer.query(
        "MATCH (p:Project {id: $id}) SET p.base_commit = $base, p.dirty_paths = $dirty",
        {"id": proj_id, "base": head, "dirty": json.dumps(sorted(paths))},
    )
