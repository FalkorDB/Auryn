"""Git-tracked (and directory) Go ingestion: moves a project's Go subgraph to a target
commit/tree by fully recomputing the target state and diffing it against the graph
(graph_sync.compute_delta) -- the same recompute-and-delta model as the Java/C/C#/Python
ingestors (resolution is global -- the package-wide type/func/method index -- so a file-level
diff can't tell which untouched files a change affects).

Fast engine only for now: tree-sitter-go streams each `.go` blob (no disk materialization, no
build config) -> RawFile -> resolution.resolve(). A deep go/types or gopls pass (structural
IMPLEMENTS, interface-dispatch/cross-file calls, inferred local-variable types) would be added
as the GitLanguage's `deep_sync` (see plugin.py). Registered as the `go` GitLanguage for the
multi-language endpoint (core/ingest.py) in both ingest_repository (git) and ingest_directory
(local)."""

from __future__ import annotations

import itertools
import logging
import time
from dataclasses import dataclass

from auryn.core.graph_model import GraphNode
from auryn.core.graph_sync import stream_apply
from auryn.core.parallel import parse_blobs_ordered
from auryn.core.registry import LanguageIngestResult
from auryn.ingestors.go import gomod, resolution
from auryn.ingestors.go.file_parser import parse_source
from auryn.ingestors.go.ids import module_id, project_id
from auryn.ingestors.go.plugin import EXCLUDED_DIR_NAMES as _EXCLUDED_DIR_NAMES
from auryn.ingestors.go.plugin import FILE_SUFFIXES as _FILE_SUFFIXES
from auryn.ingestors.go.schema import NODE_SCHEMAS

logger = logging.getLogger(__name__)


@dataclass
class SyncStats:
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


# Two-phase depth (mirrors Rust/C#/Python/TS): the fast tree-sitter pass stamps the Project
# `depth="fast"`, the deep scip-go pass `depth="deep"`. A re-run short-circuits only when the commit
# matches AND the existing depth is at least the requested one -- so a fast re-run never downgrades a deep
# graph, but the deep phase still runs right after the fast one at the same commit.
_DEPTH_RANK = {None: 0, "": 0, "fast": 1, "deep": 2}


def _project_node(proj_id: str, project_name: str, repo_uri: str, target_sha: str, ref: str | None,
                  depth: str) -> GraphNode:
    return GraphNode(
        "Project", {"id": proj_id},
        {"name": project_name, "repo_uri": repo_uri, "commit_sha": target_sha, "ref": ref, "depth": depth},
    )


def sync_at(writer, source, target_sha: str, project_name: str, repo_uri: str, ref: str | None,
            engine: str = "treesitter") -> SyncStats:
    """Recompute the whole Go subgraph at `target_sha` and delta it onto the graph. `source` is
    duck-typed (GitRepoCache or LocalDirectorySource). `engine`: "treesitter" (fast, blob-streamed) or
    "scip_go" (deep, SCIP over an on-disk module checkout, falling back to tree-sitter if scip-go or a
    loadable module is unavailable)."""
    started = time.monotonic()
    proj_id = project_id(project_name)
    requested_depth = "deep" if engine == "scip_go" else "fast"

    writer.setup_schema(NODE_SCHEMAS)  # brings the graph key into existence before the first read
    current_sha = writer.get_property("Project", {"id": proj_id}, "commit_sha")
    current_depth = writer.get_property("Project", {"id": proj_id}, "depth")
    if current_sha == target_sha and _DEPTH_RANK.get(current_depth, 0) >= _DEPTH_RANK[requested_depth]:
        return SyncStats(commit_sha=target_sha, elapsed_seconds=time.monotonic() - started)

    # go.mod module map (module_dir -> module_path), from the manifests at the target. It turns each
    # package dir into its canonical import path (the cross-repo linkage key); the node ids stay
    # dir-keyed (pure path), so a package's identity doesn't depend on it.
    modules = _read_modules(source, target_sha)
    module_map = gomod.build_module_map(modules)

    # Incremental resync (ADR 0005, authored-shared engine) -- fast engine only; the deep pass always
    # fully re-analyzes (scip-go is whole-module), like Rust/Roslyn. GUARD: a go.mod STRUCTURAL change
    # (a module path added/removed/renamed) shifts the import paths -- and thus the linkage keys -- of
    # files that did NOT change, which an incremental pass would not re-emit; fall back to a full
    # recompute so incremental == full always holds.
    if (engine == "treesitter" and current_sha and current_sha != target_sha
            and _module_map_from_graph(writer, proj_id) == module_map):
        incr = _route(writer, source, target_sha, ref, current_sha,
                      project_name=project_name, repo_uri=repo_uri)
        if incr is not None:
            # A fast incremental resync degrades a deep graph -> stamp depth="fast" so a later deep phase
            # re-runs (else commit==target & depth=="deep" would short-circuit it, leaving changes shallow).
            writer.query("MATCH (p:Project {id: $id}) SET p.depth = 'fast'", {"id": proj_id})
            _reconcile_gomod(writer, proj_id, modules)
            return _finish(writer, proj_id, source, incr)

    entries = [
        (path, blob_sha)
        for suffix in _FILE_SUFFIXES
        for path, blob_sha in source.list_files(target_sha, suffix, set(_EXCLUDED_DIR_NAMES))
    ]
    files = parse_blobs_ordered(
        entries, source.read_blob, parse_source,
        preload=["auryn.ingestors.go.file_parser"], logger=logger,
    )

    if engine == "scip_go":
        is_upgrade = current_sha == target_sha and current_depth == "fast"
        deep = _deep_sync_at(writer, source, target_sha, files, module_map, modules, proj_id, project_name,
                             repo_uri, ref, started, is_upgrade=is_upgrade, entry_sha=current_sha)
        if deep is not None:
            return deep
        # scip-go unavailable / analyzed nothing -> fall through to the tree-sitter fast pass.

    project_node = _project_node(proj_id, project_name, repo_uri, target_sha, ref, "fast")

    def make_target():  # re-iterable: stream_apply runs it once for nodes, once for edges
        return itertools.chain([project_node], resolution.resolve(files, proj_id, module_map))

    result = _apply(writer, proj_id, make_target, target_sha, started, files_parsed=len(files))
    _reconcile_gomod(writer, proj_id, modules)
    return _finish(writer, proj_id, source, result)


def _is_name_calls_edge(item) -> bool:
    return (type(item).__name__ == "GraphEdge" and item.rel_type == "CALLS"
            and (item.properties or {}).get("resolution") == "name")


def _deep_sync_at(writer, source, target_sha: str, files, module_map, modules, proj_id: str,
                  project_name: str, repo_uri: str, ref: str | None, started: float, *,
                  is_upgrade: bool, entry_sha) -> "SyncStats | None":
    """Deep pass: resolve CALLS precisely via scip-go's SCIP index, layered over the fast pass's
    structural subgraph (same node ids). Returns None (caller falls back to tree-sitter) if scip-go can't
    run or analyzed none of our callables. The overlay drops a fast name-CALLS only where deep IDENTIFIED
    that intra-project call (a resolved deep edge replaces it, or it was a wrong local guess for an
    external callee); on an upgrade (fast->deep at the same commit) extern:// placeholders are left alone
    (diff_extern_edges=False). Go's fast pass omits self-CALLS, so deep does too (no emit_self_calls)."""
    try:
        from auryn.core.scip_resolve import resolve_calls
    except ImportError:  # a plugin-only upgrade left an older auryn-core without the resolver
        logger.warning("go deep pass: this auryn-core lacks core.scip_resolve (upgrade auryn-core); "
                       "falling back to tree-sitter")
        return None
    from auryn.ingestors.go import scip_go

    def moved() -> "str | None":
        live = writer.get_property("Project", {"id": proj_id}, "commit_sha")
        return str(live) if live is not None and live != entry_sha else None

    def _fall_back() -> "SyncStats | None":
        # deep can't proceed: no-op on a same-commit upgrade (the fast graph already exists here), else
        # None to fall through and build the fast graph; never roll a newer concurrent revision back.
        live = moved()
        if live is not None or is_upgrade:
            return SyncStats(commit_sha=live or target_sha, elapsed_seconds=time.monotonic() - started)
        return None

    try:
        # scip-go needs an on-disk module checkout. A git source yields an immutable checkout at
        # target_sha; a LOCAL directory yields the live tree (as the fast parse also reads it), so an edit
        # landing between the parse and this SCIP read can skew the join -- worst case a few calls fall
        # back to fast. A content snapshot would close that live-dir window (v2).
        with source.working_tree(target_sha) as workdir:
            scip_bytes = scip_go.run_scip(str(workdir))
    except scip_go.ScipGoUnavailable as exc:
        logger.warning("go deep pass unavailable (%s); falling back to tree-sitter", exc)
        return _fall_back()

    locations = resolution.callable_locations(files, proj_id)
    # NO require_syntactic_call for Go: its fast parser omits a RawCall for a chained-selector call
    # (`svc.store.Put()` -- a receiver too complex to name), so gating on the fast call-name set would drop
    # those genuine deep-resolved method calls (the deep pass's main win). The cost is that a function used
    # as a VALUE could yield a spurious edge; recovering that precision needs the fast parser to record
    # every call-site name (shared v2 follow-up with the line-precise work).
    deep = resolve_calls(scip_bytes, locations)
    if locations and not deep.covered_ids:
        # scip-go ran but analyzed none of our callables (module won't load, no toolchain): don't make a
        # hollow depth="deep" claim -- fall back to the fast graph.
        logger.warning("go deep pass: scip-go covered no callables; falling back to tree-sitter")
        return _fall_back()

    loc_by_id = {loc.id: loc for loc in locations}
    # Keep the project node at "fast" in the delta; depth is bumped to "deep" by an ATOMIC guarded write
    # AFTER the delta lands, so a concurrent ingest that moved the commit can't be overwritten with a stale
    # "deep" claim.
    project_node = _project_node(proj_id, project_name, repo_uri, target_sha, ref, "fast")

    def make_target():  # re-iterable: stream_apply runs it once for nodes, once for edges
        yield project_node
        for item in resolution.resolve(files, proj_id, module_map):
            if _is_name_calls_edge(item):
                frm = item.from_key["id"]
                callee = loc_by_id.get(item.to_key["id"])
                if (frm in deep.covered_ids and callee is not None
                        and callee.name in deep.seen_by_caller.get(frm, set())):
                    continue  # deep identified this call -> drop the fast name-edge (deep edge replaces it)
            yield item
        yield from deep.edges

    live = moved()  # freshness guard before the write: discard a stale revision rather than clobber a newer one
    if live is not None:
        logger.warning("go deep pass: graph moved to %s while analyzing %s; discarding stale result",
                       live, target_sha)
        return SyncStats(commit_sha=live, elapsed_seconds=time.monotonic() - started)

    result = _apply(writer, proj_id, make_target, target_sha, started, files_parsed=len(files),
                    diff_extern_edges=not is_upgrade)
    # Atomically stamp depth="deep" only if the graph is STILL at this commit (GraphWriter.set_if).
    writer.set_if("Project", proj_id, "depth", "deep", "commit_sha", target_sha)
    logger.info("go deep: %d resolved / %d external CALLS over %d/%d callables covered",
                deep.metrics["resolved"], deep.metrics["external"], len(deep.covered_ids), len(locations))
    _reconcile_gomod(writer, proj_id, modules)
    return _finish(writer, proj_id, source, result)


def _route(writer, source, target_sha, ref, current_sha, *, project_name, repo_uri) -> "SyncStats | None":
    """Attempt an incremental resync via the shared engine's git routing; return SyncStats or None
    to fall back to the full recompute. GO_SPEC sets `provenance_rels`, so route_incremental
    dispatches to the authored-shared engine (incremental_sync_authored)."""
    from auryn.core.incremental_scoped import route_incremental
    from auryn.ingestors.go.incremental import GO_SPEC

    s = route_incremental(
        writer, GO_SPEC, source, target_sha, ref, current_sha,
        project_name=project_name, repo_uri=repo_uri, logger=logger,
    )
    if s is None:
        return None
    return SyncStats(
        nodes_added=s.nodes_added, nodes_updated=s.nodes_updated, nodes_removed=s.nodes_removed,
        edges_added=s.edges_added, edges_updated=s.edges_updated, edges_removed=s.edges_removed,
        commit_sha=s.commit_sha, elapsed_seconds=s.elapsed_seconds,
        files_parsed=s.files_parsed, files_cached=s.files_cached,
    )


def _finish(writer, proj_id: str, source, result: SyncStats) -> SyncStats:
    """Record the working-tree base for the next save-triggered incremental resync (no-op for
    git/plain-dir sources)."""
    from auryn.core.incremental_scoped import stamp_worktree
    from auryn.core.local_source import WorkingTreeSource

    if isinstance(source, WorkingTreeSource):
        stamp_worktree(writer, proj_id, source, _FILE_SUFFIXES)
    return result


def _apply(writer, proj_id: str, make_target, target_sha: str, started: float, files_parsed: int,
           *, diff_extern_edges: bool = True) -> SyncStats:
    # Cross-repo reconciliation (ADR 0004): fold placeholder<->definer pairs among the linkages
    # this ingest touched -- targeted index seeks, no scan. No-op unless the graph holds >1 repo.
    from auryn.core.linkage import reconcile_linkages

    # stream_apply writes the delta without materialising the target graph, and returns the linkage strings
    # collected inline (see graph_sync.stream_apply / core.linkage). The deep pass passes
    # diff_extern_edges=False on an upgrade so it doesn't prune the fast pass's extern:// placeholder CALLS
    # (it can't author cross-repo edges; reconcile_linkages heals them later).
    labels = [s.label for s in NODE_SCHEMAS]
    stats, reals, placeholders = stream_apply(writer, proj_id, labels, make_target,
                                              diff_extern_edges=diff_extern_edges)
    reconcile_linkages(writer, reals, placeholders)

    return SyncStats(
        nodes_added=stats.nodes_added,
        nodes_updated=stats.nodes_updated,
        nodes_removed=stats.nodes_removed,
        edges_added=stats.edges_added,
        edges_updated=stats.edges_updated,
        edges_removed=stats.edges_removed,
        commit_sha=target_sha,
        elapsed_seconds=time.monotonic() - started,
        files_parsed=files_parsed,
        files_cached=0,
    )


_GO_MOD = "go.mod"


def _read_modules(source, target_sha: str) -> list:
    """Parse every `go.mod` at the target into a list of `gomod.GoModule`."""
    mods = []
    for rel, blob in source.list_files(target_sha, _GO_MOD, set(_EXCLUDED_DIR_NAMES)):
        mdir = rel[: -len(_GO_MOD)].rstrip("/")
        mods.append(gomod.parse_gomod(mdir, source.read_blob(blob)))
    return mods


def _module_map_from_graph(writer, proj_id: str) -> dict:
    """Reconstruct `{module_dir: module_path}` from the graph's Module nodes -- compared against the
    target manifests to decide whether a go.mod structural change forces a full recompute."""
    lo, hi = proj_id + "/", proj_id + "/" + chr(0xFFFF)
    return {
        str(mdir): str(mpath) for mdir, mpath in writer.ro_query(
            "MATCH (m:Module) WHERE m.id >= $lo AND m.id < $hi RETURN m.dir, m.name", {"lo": lo, "hi": hi}
        ).result_set
    }


def _reconcile_gomod(writer, proj_id: str, modules: list) -> None:
    """Layer the manifest-only facts onto the Module tier (go version + crate-to-crate require
    DEPENDS_ON), deterministically from the go.mod set -- so the differential oracle's
    incremental == full holds (the delta callbacks can't read a go.mod; this step, which has source,
    is where that data enters). Runs in BOTH the full and incremental paths.

    A Module is MERGED (not just enriched): resolution authors it from the module map and the
    aggregation refcount GCs it when its last .go file goes, but a manifest-backed module must
    OUTLIVE its source files (a full ingest emits it from the go.mod regardless) -- so we recreate it
    here, stamping the shared `:Entity` label the normal upsert path adds, and (re)attach it to the
    Project. `_module_map_from_graph` therefore always mirrors the go.mod set, keeping the structural-
    change fallback exact."""
    path_to_dir = {m.module_path: m.dir for m in modules if m.module_path}
    desired: set[tuple[str, str]] = set()
    for m in modules:
        if m.module_path is None:
            continue
        external = []
        for req in m.requires:
            if req.indirect:
                continue
            if req.module_path in path_to_dir and path_to_dir[req.module_path] != m.dir:
                desired.add((module_id(proj_id, m.dir), module_id(proj_id, path_to_dir[req.module_path])))
            elif req.module_path not in path_to_dir:
                external.append(f"{req.module_path} {req.version}".strip())
        writer.query(
            "MERGE (mod:Module {id: $id}) "
            "SET mod:Entity, mod.name = $name, mod.qualified_name = $name, mod.dir = $dir, "
            "mod.go_version = $gv, mod.external_requires = $ext "
            "WITH mod MATCH (p:Project {id: $proj}) MERGE (p)-[:HAS_MODULE]->(mod)",
            {"id": module_id(proj_id, m.dir), "name": m.module_path, "dir": m.dir,
             "proj": proj_id, "gv": m.go_version, "ext": sorted(set(external))},
        )

    existing = writer.ro_query(
        "MATCH (a:Module)-[:DEPENDS_ON]->(b:Module) WHERE a.id STARTS WITH $pre RETURN a.id, b.id",
        {"pre": proj_id + "/module"},
    ).result_set
    for a, b in existing:
        if (str(a), str(b)) not in desired:
            writer.query("MATCH (a:Module {id: $a})-[r:DEPENDS_ON]->(b:Module {id: $b}) DELETE r",
                         {"a": str(a), "b": str(b)})
    for a, b in sorted(desired):
        writer.query(
            "MATCH (a:Module {id: $a}), (b:Module {id: $b}) "
            "MERGE (a)-[r:DEPENDS_ON]->(b) SET r.kind = 'gomod', r.resolution = 'name'",
            {"a": a, "b": b},
        )


def _git_language_sync(
    writer,
    source,
    target_sha: str,
    *,
    repo_uri: str,
    project_name: str,
    solution_name: str | None,
    ref: str | None,
    cache_root=None,
    engine: str = "treesitter",
) -> LanguageIngestResult:
    """GitLanguage adapter for the multi-language endpoint (Go has no solution/cache knobs --
    accepted for the shared interface and ignored). `engine` selects the fast or deep pass."""
    s = sync_at(writer, source, target_sha, project_name=project_name, repo_uri=repo_uri, ref=ref,
                engine=engine)
    return LanguageIngestResult(
        nodes_added=s.nodes_added,
        nodes_updated=s.nodes_updated,
        nodes_removed=s.nodes_removed,
        edges_added=s.edges_added,
        edges_updated=s.edges_updated,
        edges_removed=s.edges_removed,
        files_parsed=s.files_parsed,
        files_cached=s.files_cached,
        commit_sha=s.commit_sha,
        elapsed_seconds=s.elapsed_seconds,
    )


def treesitter_language_sync(writer, source, target_sha, **kwargs) -> LanguageIngestResult:
    """Fast-phase GitLanguage `sync` thunk (the default engine)."""
    return _git_language_sync(writer, source, target_sha, engine="treesitter", **kwargs)


def scip_go_language_sync(writer, source, target_sha, **kwargs) -> LanguageIngestResult:
    """Deep-phase GitLanguage `deep_sync` thunk (scip-go / SCIP, degrading to the fast pass)."""
    return _git_language_sync(writer, source, target_sha, engine="scip_go", **kwargs)


# The GitLanguage is registered by the import-light plugin.py (the entry-point target), whose lazy
# `sync`/`deep_sync` thunks call the adapters above -- so importing this tree-sitter engine module (and,
# for the deep phase, scip-go) is deferred until a source actually contains Go files.
