"""Git-tracked (and directory) Java ingestion: moves a project's Java subgraph to a target
commit/tree by fully recomputing the target state and diffing it against the graph
(graph_sync.compute_delta) -- the same recompute-and-delta model as the C/C#/Python ingestors
(resolution is global -- the package-wide type/method index -- so a file-level diff can't tell
which untouched files a change affects).

Fast engine only for now: tree-sitter-java streams each `.java` blob (no disk materialization,
no build config) -> RawFile -> resolution.resolve(). A deep Eclipse JDT / javac pass (resolved
types, inherited-member calls, precise overload selection) would be added as the GitLanguage's
`deep_sync` (see plugin.py). Registered as the `java` GitLanguage for the multi-language
endpoint (core/ingest.py) in both ingest_repository (git) and ingest_directory (local)."""

from __future__ import annotations

import itertools
import logging
import time
from dataclasses import dataclass

from auryn.core.parallel import parse_blobs_ordered
from auryn.core.graph_model import GraphNode
from auryn.core.graph_sync import stream_apply
from auryn.core.registry import LanguageIngestResult
from auryn.ingestors.java import resolution
from auryn.ingestors.java.file_parser import parse_source
from auryn.ingestors.java.ids import project_id
from auryn.ingestors.java.plugin import EXCLUDED_DIR_NAMES as _EXCLUDED_DIR_NAMES
from auryn.ingestors.java.plugin import FILE_SUFFIXES as _FILE_SUFFIXES
from auryn.ingestors.java.schema import NODE_SCHEMAS

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


# Two-phase depth (mirrors Rust/C#/Python/TS/Go): the fast tree-sitter pass stamps the Project
# `depth="fast"`, the deep scip-java pass `depth="deep"`. A re-run short-circuits only when the commit
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
    """Recompute the whole Java subgraph at `target_sha` and delta it onto the graph. `source` is
    duck-typed (GitRepoCache or LocalDirectorySource). `engine`: "treesitter" (fast, blob-streamed) or
    "scip_java" (deep, SCIP over an on-disk checkout scip-java builds, falling back to tree-sitter if
    scip-java or a buildable project is unavailable)."""
    started = time.monotonic()
    proj_id = project_id(project_name)
    requested_depth = "deep" if engine == "scip_java" else "fast"

    writer.setup_schema(NODE_SCHEMAS)  # brings the graph key into existence before the first read
    current_sha = writer.get_property("Project", {"id": proj_id}, "commit_sha")
    current_depth = writer.get_property("Project", {"id": proj_id}, "depth")
    if current_sha == target_sha and _DEPTH_RANK.get(current_depth, 0) >= _DEPTH_RANK[requested_depth]:
        return SyncStats(commit_sha=target_sha, elapsed_seconds=time.monotonic() - started)

    # Incremental resync (ADR 0005, authored-shared engine) -- fast engine only; the deep pass always
    # fully re-analyzes (scip-java rebuilds the whole project), like Rust/Roslyn. Any failure falls back
    # to the full recompute below (which leaves the graph untouched until it writes).
    if engine == "treesitter" and current_sha and current_sha != target_sha:
        incr = _route(writer, source, target_sha, ref, current_sha,
                      project_name=project_name, repo_uri=repo_uri)
        if incr is not None:
            # A fast incremental resync degrades a deep graph -> stamp depth="fast" so a later deep phase
            # re-runs (else commit==target & depth=="deep" would short-circuit it, leaving changes shallow).
            writer.query("MATCH (p:Project {id: $id}) SET p.depth = 'fast'", {"id": proj_id})
            return _finish(writer, proj_id, source, incr)

    entries = [
        (path, blob_sha)
        for suffix in _FILE_SUFFIXES
        for path, blob_sha in source.list_files(target_sha, suffix, set(_EXCLUDED_DIR_NAMES))
    ]
    files = parse_blobs_ordered(
        entries, source.read_blob, parse_source,
        preload=["auryn.ingestors.java.file_parser"], logger=logger,
    )

    if engine == "scip_java":
        is_upgrade = current_sha == target_sha and current_depth == "fast"
        deep = _deep_sync_at(writer, source, target_sha, files, proj_id, project_name, repo_uri, ref,
                             started, is_upgrade=is_upgrade, entry_sha=current_sha)
        if deep is not None:
            return deep
        # scip-java unavailable / build failed / analyzed nothing -> fall through to the fast pass.

    project_node = _project_node(proj_id, project_name, repo_uri, target_sha, ref, "fast")

    def make_target():  # re-iterable: stream_apply runs it once for nodes, once for edges
        return itertools.chain([project_node], resolution.resolve(files, proj_id))

    return _finish(writer, proj_id, source,
                   _apply(writer, proj_id, make_target, target_sha, started, files_parsed=len(files)))


def _is_name_calls_edge(item) -> bool:
    return (type(item).__name__ == "GraphEdge" and item.rel_type == "CALLS"
            and (item.properties or {}).get("resolution") == "name")


def _deep_sync_at(writer, source, target_sha: str, files, proj_id: str, project_name: str, repo_uri: str,
                  ref: str | None, started: float, *, is_upgrade: bool, entry_sha) -> "SyncStats | None":
    """Deep pass: resolve CALLS precisely via scip-java's SCIP index, layered over the fast pass's
    structural subgraph (same node ids). Returns None (caller falls back to tree-sitter) if scip-java can't
    run, the build fails, or it analyzed none of our callables. The overlay drops a fast name-CALLS only
    where deep IDENTIFIED that intra-project call; on an upgrade (fast->deep at the same commit) extern://
    placeholders are left alone (diff_extern_edges=False). Java's fast pass records every call-site name
    and emits self-CALLS, so deep gates on syntactic calls AND authors self-edges (like TS)."""
    try:
        from auryn.core.scip_resolve import resolve_calls
    except ImportError:  # a plugin-only upgrade left an older auryn-core without the resolver
        logger.warning("java deep pass: this auryn-core lacks core.scip_resolve (upgrade auryn-core); "
                       "falling back to tree-sitter")
        return None
    from auryn.ingestors.java import scip_java

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
        # scip-java needs an on-disk checkout (it RUNS THE BUILD). A git source yields an immutable checkout
        # at target_sha; a LOCAL directory yields the live tree, so the build writes target/build dirs into
        # it (excluded from the fast parse) and an edit between the parse and this read can skew the join.
        with source.working_tree(target_sha) as workdir:
            scip_bytes = scip_java.run_scip(str(workdir))
    except scip_java.ScipJavaUnavailable as exc:
        logger.warning("java deep pass unavailable (%s); falling back to tree-sitter", exc)
        return _fall_back()

    locations = resolution.callable_locations(files, proj_id)
    deep = resolve_calls(scip_bytes, locations, require_syntactic_call=True, emit_self_calls=True)
    if locations and not deep.covered_ids:
        logger.warning("java deep pass: scip-java covered no callables; falling back to tree-sitter")
        return _fall_back()

    loc_by_id = {loc.id: loc for loc in locations}
    project_node = _project_node(proj_id, project_name, repo_uri, target_sha, ref, "fast")

    def make_target():  # re-iterable: stream_apply runs it once for nodes, once for edges
        yield project_node
        for item in resolution.resolve(files, proj_id):
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
        logger.warning("java deep pass: graph moved to %s while analyzing %s; discarding stale result",
                       live, target_sha)
        return SyncStats(commit_sha=live, elapsed_seconds=time.monotonic() - started)

    result = _apply(writer, proj_id, make_target, target_sha, started, files_parsed=len(files),
                    diff_extern_edges=not is_upgrade)
    writer.set_if("Project", proj_id, "depth", "deep", "commit_sha", target_sha)  # atomic guarded bump
    logger.info("java deep: %d resolved / %d external CALLS over %d/%d callables covered",
                deep.metrics["resolved"], deep.metrics["external"], len(deep.covered_ids), len(locations))
    return _finish(writer, proj_id, source, result)


def _route(writer, source, target_sha, ref, current_sha, *, project_name, repo_uri) -> "SyncStats | None":
    """Attempt an incremental resync via the shared engine's git routing; return SyncStats or None to
    fall back to the full recompute. JAVA_SPEC sets `provenance_rels`, so route_incremental dispatches
    to the authored-shared engine (incremental_sync_authored)."""
    from auryn.core.incremental_scoped import route_incremental
    from auryn.ingestors.java.incremental import JAVA_SPEC

    s = route_incremental(
        writer, JAVA_SPEC, source, target_sha, ref, current_sha,
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
    """GitLanguage adapter for the multi-language endpoint (Java has no solution/cache knobs --
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


def scip_java_language_sync(writer, source, target_sha, **kwargs) -> LanguageIngestResult:
    """Deep-phase GitLanguage `deep_sync` thunk (scip-java / SCIP, degrading to the fast pass)."""
    return _git_language_sync(writer, source, target_sha, engine="scip_java", **kwargs)


# The GitLanguage is registered by the import-light plugin.py (the entry-point target), whose lazy
# `sync`/`deep_sync` thunks call the adapters above -- so importing this tree-sitter engine module (and,
# for the deep phase, scip-java) is deferred until a source actually contains Java files.
