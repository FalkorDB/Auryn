"""Git-tracked (and directory) C++ ingestion: moves a project's C++ subgraph to a target
commit/tree by fully recomputing the target state and diffing it against the graph
(graph_sync.compute_delta) -- the same recompute-and-delta model as the C/C#/Java/Python
ingestors (resolution is global -- the namespace-wide type/method/function index and the
out-of-line member-definition wiring -- so a file-level diff can't tell which untouched files a
change affects).

Fast engine only for now: tree-sitter-cpp streams each C++ blob (no disk materialization, no
build config) -> RawFile -> resolution.resolve(). A deep libclang pass (template instantiation,
overload resolution, ADL, precise header linking, cross-TU calls) would be added as the
GitLanguage's `deep_sync` (see plugin.py). Registered as the `cpp` GitLanguage for the
multi-language endpoint (core/ingest.py) in both ingest_repository (git) and ingest_directory
(local)."""

from __future__ import annotations

import itertools
import logging
import time
from dataclasses import dataclass

from auryn.core.parallel import parse_blobs_ordered
from auryn.core.graph_model import GraphNode
from auryn.core.graph_sync import stream_apply
from auryn.core.registry import LanguageIngestResult
from auryn.ingestors.cpp import resolution
from auryn.ingestors.cpp.file_parser import parse_source
from auryn.ingestors.cpp.ids import project_id
from auryn.ingestors.cpp.plugin import EXCLUDED_DIR_NAMES as _EXCLUDED_DIR_NAMES
from auryn.core.cpp_headers import HEADER_SUFFIX, is_cpp_header_blob
from auryn.ingestors.cpp.plugin import FILE_SUFFIXES as _FILE_SUFFIXES
from auryn.ingestors.cpp.schema import NODE_SCHEMAS

logger = logging.getLogger(__name__)

# Provenance-format marker stamped on the Project node once a graph has been built with the
# File-[:DEFINES|DECLARES]->entity provenance the authored incremental engine reads (ADR 0005). A C++
# graph built by an OLDER Auryn (full-recompute only, NO provenance edges) lacks it; the incremental
# route is gated on this marker so such a legacy graph does one full recompute first (which stamps it),
# instead of taking the incremental path against a graph the engine can't scope. Bump on a format change.
_DELTA_FORMAT = 1


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


# Two-phase depth (mirrors Rust/C#/Python/TS/Go/Java): the fast tree-sitter pass stamps the Project
# `depth="fast"`, the deep scip-clang pass `depth="deep"`. A re-run short-circuits only when the commit
# matches AND the existing depth is at least the requested one -- so a fast re-run never downgrades a deep
# graph, but the deep phase still runs right after the fast one at the same commit.
_DEPTH_RANK = {None: 0, "": 0, "fast": 1, "deep": 2}


def _project_node(proj_id: str, project_name: str, repo_uri: str, target_sha: str, ref: str | None,
                  depth: str) -> GraphNode:
    # stream_apply merges props, so adding `depth` leaves the provenance `delta_format` marker intact.
    return GraphNode(
        "Project", {"id": proj_id},
        {"name": project_name, "repo_uri": repo_uri, "commit_sha": target_sha, "ref": ref, "depth": depth},
    )


def sync_at(writer, source, target_sha: str, project_name: str, repo_uri: str, ref: str | None,
            engine: str = "treesitter") -> SyncStats:
    """Recompute the whole C++ subgraph at `target_sha` and delta it onto the graph. `source` is
    duck-typed (GitRepoCache or LocalDirectorySource). `engine`: "treesitter" (fast, blob-streamed) or
    "scip_clang" (deep, SCIP over an on-disk checkout with a compile_commands.json, falling back to
    tree-sitter if scip-clang or a compdb is unavailable)."""
    started = time.monotonic()
    proj_id = project_id(project_name)
    requested_depth = "deep" if engine == "scip_clang" else "fast"

    writer.setup_schema(NODE_SCHEMAS)  # brings the graph key into existence before the first read
    current_sha = writer.get_property("Project", {"id": proj_id}, "commit_sha")
    current_depth = writer.get_property("Project", {"id": proj_id}, "depth")
    if current_sha == target_sha and _DEPTH_RANK.get(current_depth, 0) >= _DEPTH_RANK[requested_depth]:
        return SyncStats(commit_sha=target_sha, elapsed_seconds=time.monotonic() - started)

    # Incremental resync (ADR 0005, authored-shared engine) -- fast engine only; the deep pass always
    # fully re-analyzes (scip-clang is whole-TU). Only route incremental when the existing graph carries
    # the provenance format (a legacy graph has commit_sha but no DEFINES/DECLARES provenance).
    has_provenance = writer.get_property("Project", {"id": proj_id}, "delta_format") == _DELTA_FORMAT
    if engine == "treesitter" and current_sha and current_sha != target_sha and has_provenance:
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
    # `.h` is not in C++'s suffix list (it's C's by default), but a `.h` with C++ constructs IS a C++
    # header -- the dominant case in real C++ (protobuf/LLVM/Chromium use `.h`). Claim those here so the
    # class declared in the header joins its `.cc` definition under one `cpp://` node; the C ingestor
    # skips the same set (see core.cpp_headers / c.git_sync).
    # KNOWN RESIDUAL (narrow; PR #16 review): the save-watcher derives its trigger extensions from
    # registered file_suffixes (core/watcher.py). `.h` reaches it via the C ingestor's suffix list, so a
    # normal (C + C++) install resyncs on `.h` edits. In a C++-ONLY install (no auryn-c), `.h` is in no
    # suffix list, so a save-triggered watcher won't resync an edited C++ `.h` until a manual/commit sync.
    # The robust fix (content-gated `.h` as a first-class C++ suffix) lands with the incremental `.h` glue.
    entries += [
        (path, blob_sha)
        for path, blob_sha in source.list_files(target_sha, HEADER_SUFFIX, set(_EXCLUDED_DIR_NAMES))
        if is_cpp_header_blob(source.read_blob(blob_sha))
    ]
    files = parse_blobs_ordered(
        entries, source.read_blob, parse_source,
        preload=["auryn.ingestors.cpp.file_parser"], logger=logger,
    )

    if engine == "scip_clang":
        is_upgrade = current_sha == target_sha and current_depth == "fast"
        deep = _deep_sync_at(writer, source, target_sha, files, proj_id, project_name, repo_uri, ref,
                             started, is_upgrade=is_upgrade, entry_sha=current_sha)
        if deep is not None:
            return deep
        # scip-clang unavailable / no compdb / analyzed nothing -> fall through to the fast pass.

    project_node = _project_node(proj_id, project_name, repo_uri, target_sha, ref, "fast")

    def make_target():  # re-iterable: stream_apply runs it once for nodes, once for edges
        return itertools.chain([project_node], resolution.resolve(files, proj_id))

    return _finish(writer, proj_id, source,
                   _apply(writer, proj_id, make_target, target_sha, started, files_parsed=len(files)))




def _deep_sync_at(writer, source, target_sha: str, files, proj_id: str, project_name: str, repo_uri: str,
                  ref: str | None, started: float, *, is_upgrade: bool, entry_sha) -> "SyncStats | None":
    """Deep pass: resolve CALLS precisely via scip-clang's SCIP index (overload / virtual dispatch / cross-
    file), layered over the fast pass's structural subgraph (same node ids). Returns None (caller falls back
    to tree-sitter) if scip-clang can't run, no compile_commands.json is found, or it analyzed none of our
    callables. The overlay drops a fast name-CALLS only where deep IDENTIFIED that intra-project call; on an
    upgrade extern:// placeholders are left alone (diff_extern_edges=False). C++'s fast parser omits a call
    name for a constructor-construction / complex receiver (so require_syntactic_call would drop those), but
    it emits self-CALLS -- so deep does NOT gate but DOES author self-edges."""
    try:
        from auryn.core.deep_sync import is_name_calls_edge as _is_name_calls_edge, should_drop_name_edge
        from auryn.core.scip_resolve import resolve_calls
    except ImportError:  # a plugin-only upgrade left an older auryn-core without the resolver
        logger.warning("cpp deep pass: this auryn-core lacks core.scip_resolve (upgrade auryn-core); "
                       "falling back to tree-sitter")
        return None
    from auryn.ingestors.cpp import scip_clang

    def moved() -> "str | None":
        live = writer.get_property("Project", {"id": proj_id}, "commit_sha")
        return str(live) if live is not None and live != entry_sha else None

    def _fall_back() -> "SyncStats | None":
        live = moved()
        if live is not None or is_upgrade:
            return SyncStats(commit_sha=live or target_sha, elapsed_seconds=time.monotonic() - started)
        return None

    try:  # cheap opt-in gate BEFORE the expensive working_tree() checkout: skip immediately if not opted in
        scip_clang._tool_command()
    except scip_clang.ScipClangUnavailable as exc:
        logger.warning("cpp deep pass not opted in (%s); falling back to tree-sitter", exc)
        return _fall_back()

    try:
        # scip-clang needs an on-disk checkout + a compile_commands.json whose absolute paths match it (so
        # in practice a LOCAL-directory source, the live tree where the build generated the compdb). It
        # reads the sources read-only (no object files), writing only the index to a temp path.
        with source.working_tree(target_sha) as workdir:
            scip_bytes = scip_clang.run_scip(str(workdir))
    except scip_clang.ScipClangUnavailable as exc:
        logger.warning("cpp deep pass unavailable (%s); falling back to tree-sitter", exc)
        return _fall_back()

    locations = resolution.callable_locations(files, proj_id)
    # DELIBERATELY un-gated (no require_call_site / require_syntactic_call), like Rust -- do NOT naively flip
    # this to the line-precise gate the way Go/TS/Java did (#802). C++ construction via a DECLARATION --
    # `Circle c(2.0)` / `Circle d{3.0}`, idiomatic RAII -- parses as a declaration, not a call_expression, so
    # the fast parser records no call site for it (robustly detecting it is most-vexing-parse territory); yet
    # scip-clang resolves the constructor call, and that ctor edge is a primary C++ deep win. A line-precise
    # gate would DROP every stack/braced-construction ctor edge (verified: the fixture's `run()` has
    # `Circle c(2.0)`, and `run().calls` carries only `compute`, not `Circle`). Preprocessor macros add
    # nothing to gate against (a macro USE resolves to a macro symbol, not a `().` callable, so it is already
    # skipped; a macro-body call anchors at the `#define`, unattributed). The cost of staying un-gated is a
    # spurious edge for a function used as a VALUE (a function pointer / callback), a narrower loss than the
    # constructor edges a gate would sacrifice -- and it can't be validated end-to-end (no scip-clang in CI).
    # Gating C++ would first need construction-site capture over declarations AND scip-clang in CI to verify.
    deep = resolve_calls(scip_bytes, locations, emit_self_calls=True)
    if locations and not deep.covered_ids:
        logger.warning("cpp deep pass: scip-clang covered no callables; falling back to tree-sitter")
        return _fall_back()

    loc_by_id = {loc.id: loc for loc in locations}
    project_node = _project_node(proj_id, project_name, repo_uri, target_sha, ref, "fast")

    def make_target():  # re-iterable: stream_apply runs it once for nodes, once for edges
        yield project_node
        for item in resolution.resolve(files, proj_id):
            if _is_name_calls_edge(item) and should_drop_name_edge(item, deep, loc_by_id):
                continue  # deep identified this call -> drop the fast name-edge (deep edge replaces it)
            yield item
        yield from deep.edges

    live = moved()  # freshness guard before the write: discard a stale revision rather than clobber a newer one
    if live is not None:
        logger.warning("cpp deep pass: graph moved to %s while analyzing %s; discarding stale result",
                       live, target_sha)
        return SyncStats(commit_sha=live, elapsed_seconds=time.monotonic() - started)

    result = _apply(writer, proj_id, make_target, target_sha, started, files_parsed=len(files),
                    diff_extern_edges=not is_upgrade)
    writer.set_if("Project", proj_id, "depth", "deep", "commit_sha", target_sha)  # atomic guarded bump
    logger.info("cpp deep: %d resolved / %d external CALLS over %d/%d callables covered",
                deep.metrics["resolved"], deep.metrics["external"], len(deep.covered_ids), len(locations))
    return _finish(writer, proj_id, source, result)


def _route(writer, source, target_sha, ref, current_sha, *, project_name, repo_uri) -> "SyncStats | None":
    """Attempt an incremental resync via the shared engine's git routing (working-tree or bare-clone
    producer); return SyncStats or None to fall back to the full recompute. C++'s CPP_SPEC sets
    `provenance_rels`, so route_incremental dispatches to the authored-shared engine."""
    from auryn.core.incremental_scoped import route_incremental
    from auryn.ingestors.cpp.incremental import CPP_SPEC

    s = route_incremental(
        writer, CPP_SPEC, source, target_sha, ref, current_sha,
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
    """After any non-short-circuit sync, record the working-tree base for the next save-triggered
    incremental resync (a no-op for git/plain-dir sources)."""
    from auryn.core.incremental_scoped import stamp_worktree
    from auryn.core.local_source import WorkingTreeSource

    if isinstance(source, WorkingTreeSource):
        # Stamp `.h` too (not just _FILE_SUFFIXES): since the incremental engine now claims C++-content
        # `.h` headers, a dirty header edit must be remembered as a dirty path -- otherwise reverting an
        # uncommitted `.h` edit back to HEAD leaves no git diff AND no stamped path, so the next
        # save-triggered resync would never reprocess it and the graph would keep the edited version.
        stamp_worktree(writer, proj_id, source, _FILE_SUFFIXES + (HEADER_SUFFIX,))
    # Mark the graph as carrying the provenance format (gates the incremental route; see _DELTA_FORMAT).
    # A side-channel prop set after the Project node is written -- never part of its diffed fingerprint.
    writer.query("MATCH (p:Project {id: $id}) SET p.delta_format = $v", {"id": proj_id, "v": _DELTA_FORMAT})
    return result


def _apply(writer, proj_id: str, make_target, target_sha: str, started: float, files_parsed: int,
           *, diff_extern_edges: bool = True) -> SyncStats:
    # Cross-repo reconciliation (ADR 0004): fold placeholder<->definer pairs among the linkages
    # this ingest touched -- targeted index seeks, no scan. No-op unless the graph holds >1 repo.
    from auryn.core.linkage import reconcile_linkages

    # stream_apply writes the delta without materialising the target graph, and returns the linkage strings
    # collected inline (see graph_sync.stream_apply / core.linkage). The deep pass passes
    # diff_extern_edges=False on an upgrade so it doesn't prune the fast pass's extern:// placeholder CALLS.
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
    """GitLanguage adapter for the multi-language endpoint (C++ has no solution/cache knobs --
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


def scip_clang_language_sync(writer, source, target_sha, **kwargs) -> LanguageIngestResult:
    """Deep-phase GitLanguage `deep_sync` thunk (scip-clang / SCIP, degrading to the fast pass)."""
    return _git_language_sync(writer, source, target_sha, engine="scip_clang", **kwargs)


# The GitLanguage is registered by the import-light plugin.py (the entry-point target), whose lazy
# `sync`/`deep_sync` thunks call the adapters above -- so importing this tree-sitter engine module (and,
# for the deep phase, scip-clang) is deferred until a source actually contains C++ files.
