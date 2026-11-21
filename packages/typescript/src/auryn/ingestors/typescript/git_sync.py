"""Git-tracked (and directory) TypeScript/JavaScript ingestion: moves a project's JS/TS
subgraph to a target commit/tree by fully recomputing the target state and diffing it against
the graph (graph_sync.compute_delta) -- the same recompute-and-delta model as the
C/C#/Python ingestors (resolution is global -- imports, cross-module calls, shared type names
-- so a file-level diff can't tell which untouched modules a change affects).

Fast engine only for now: tree-sitter streams each JS/TS blob (no disk materialization, no
build config) -> RawFile -> resolution.resolve(). A deep tsserver / TS-compiler pass (resolved
types, cross-module call/return resolution) would be added as the GitLanguage's `deep_sync`
(see plugin.py). Registered as the `typescript` GitLanguage for the multi-language endpoint
(core/ingest.py) in both ingest_repository (git) and ingest_directory (local)."""

from __future__ import annotations

import itertools
import logging
import pickle
import time
from dataclasses import dataclass, field
from pathlib import Path

from auryn.core.git_source import cache_home
from auryn.core.graph_model import GraphNode
from auryn.core.graph_sync import stream_apply
from auryn.core.parallel import parse_blobs_ordered
from auryn.core.registry import LanguageIngestResult
from auryn.ingestors.typescript import resolution
from auryn.ingestors.typescript.file_parser import parse_source
from auryn.ingestors.typescript.ids import project_id
from auryn.ingestors.typescript.plugin import EXCLUDED_DIR_NAMES as _EXCLUDED_DIR_NAMES
from auryn.ingestors.typescript.plugin import FILE_SUFFIXES as _FILE_SUFFIXES
from auryn.ingestors.typescript.raw_model import RawFile
from auryn.ingestors.typescript.schema import NODE_SCHEMAS

_PARSE_CACHE_VERSION = 3
"""Bump if RawFile's shape (or what parse_source extracts) changes. Part of the cache dir
name, so a bump starts a fresh cache rather than unpickling a stale shape. v2: callable/type
dataclasses gained the shared `doc` property (leading comment). v3: RawCall gained `line` (the
callee-identifier line) for the deep pass's line-precise call-site gate."""

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
    broken_files: list[str] = field(default_factory=list)


# Two-phase depth (mirrors Rust/C#/Python): the fast tree-sitter pass stamps the Project `depth="fast"`,
# the deep scip-typescript pass `depth="deep"`. A re-run short-circuits only when the commit matches AND
# the existing depth is at least the requested one -- so a fast re-run never downgrades a deep graph, but
# the deep phase still runs right after the fast one at the same commit.
_DEPTH_RANK = {None: 0, "": 0, "fast": 1, "deep": 2}


def _project_node(proj_id: str, project_name: str, repo_uri: str, target_sha: str, ref: str | None,
                  depth: str) -> GraphNode:
    return GraphNode(
        "Project", {"id": proj_id},
        {"name": project_name, "repo_uri": repo_uri, "commit_sha": target_sha, "ref": ref, "depth": depth},
    )


def sync_at(writer, source, target_sha: str, project_name: str, repo_uri: str, ref: str | None,
            cache_root: Path | None = None, engine: str = "treesitter") -> SyncStats:
    """Move the whole JS/TS subgraph to `target_sha`. Takes the incremental path (ADR 0002)
    when a prior graph exists at a different state and git can name the change; otherwise a
    full recompute + delta. `source` is duck-typed (GitRepoCache / LocalDirectorySource /
    WorkingTreeSource). `engine`: "treesitter" (fast, blob-streamed) or "scip_typescript" (deep,
    SCIP over an on-disk checkout, falling back to tree-sitter if scip-typescript is unavailable).

    The full-recompute path is memory-bounded: files are parsed straight into the blob-sha
    cache and dropped (never all held at once), then resolution.build_plan reads that cache
    into a lightweight scope and resolution.emit_streaming emits one file at a time -- so a
    large first ingest doesn't hold every file's declaration source in memory."""
    started = time.monotonic()
    proj_id = project_id(project_name)
    requested_depth = "deep" if engine == "scip_typescript" else "fast"

    writer.setup_schema(NODE_SCHEMAS)  # brings the graph key into existence before the first read
    current_sha = writer.get_property("Project", {"id": proj_id}, "commit_sha")
    current_depth = writer.get_property("Project", {"id": proj_id}, "depth")
    if current_sha == target_sha and _DEPTH_RANK.get(current_depth, 0) >= _DEPTH_RANK[requested_depth]:
        return SyncStats(commit_sha=target_sha, elapsed_seconds=time.monotonic() - started)

    # Incremental resync (fast engine only; the deep pass always re-analyzes the whole project, like Rust/
    # Roslyn). A prior graph at a different commit is moved by the git diff + its blast radius.
    if engine == "treesitter" and current_sha and current_sha != target_sha:
        incr = _route(writer, source, target_sha, project_name, repo_uri, ref, current_sha)
        if incr is not None:
            # A fast incremental resync degrades a deep graph (changed callables drop to name resolution),
            # so stamp depth="fast" -- else a later deep phase would see commit==target & depth=="deep" and
            # short-circuit, leaving the changes shallow.
            writer.query("MATCH (p:Project {id: $id}) SET p.depth = 'fast'", {"id": proj_id})
            return _finish(writer, proj_id, source, incr)

    parse_cache_dir = (cache_root or cache_home()) / f"parse_cache_ts.v{_PARSE_CACHE_VERSION}"
    file_refs, files_parsed, files_cached = _ensure_all_cached(source, target_sha, parse_cache_dir)

    def load_fn(blob_sha: str) -> RawFile:
        cached = _load_cached(parse_cache_dir, blob_sha)
        if cached is None:  # corrupt/evicted -- re-parse (path is re-stamped at use, so "" is fine)
            cached = parse_source(source.read_blob(blob_sha), "")
            _store_cached(parse_cache_dir, blob_sha, cached)
        return cached

    plan = resolution.build_plan(file_refs, load_fn, proj_id)

    if engine == "scip_typescript":
        is_upgrade = current_sha == target_sha and current_depth == "fast"
        deep = _deep_sync_at(writer, source, target_sha, file_refs, load_fn, plan, proj_id, project_name,
                             repo_uri, ref, started, is_upgrade=is_upgrade, entry_sha=current_sha,
                             files_parsed=files_parsed, files_cached=files_cached)
        if deep is not None:
            return deep
        # scip-typescript unavailable / analyzed nothing -> fall through to the tree-sitter fast pass.

    project_node = _project_node(proj_id, project_name, repo_uri, target_sha, ref, "fast")

    def make_target():  # re-iterable: stream_apply runs it once for nodes, once for edges
        return itertools.chain([project_node], resolution.emit_streaming(plan, load_fn, proj_id))

    return _finish(writer, proj_id, source,
                   _apply(writer, proj_id, make_target, target_sha, started,
                          files_parsed=files_parsed, files_cached=files_cached, broken=plan.broken))


def _stamped(rf: RawFile, path: str) -> RawFile:
    """Re-stamp a cache-loaded RawFile with its authoritative path (as build_plan/emit_streaming do) --
    the cached object's own `.path` is re-stamped at use, so it can't be trusted for id derivation."""
    rf.path = path
    return rf


def _is_name_calls_edge(item) -> bool:
    return (type(item).__name__ == "GraphEdge" and item.rel_type == "CALLS"
            and (item.properties or {}).get("resolution") == "name")


def _deep_sync_at(writer, source, target_sha: str, file_refs, load_fn, plan, proj_id: str,
                  project_name: str, repo_uri: str, ref: str | None, started: float, *,
                  is_upgrade: bool, entry_sha, files_parsed: int, files_cached: int) -> "SyncStats | None":
    """Deep pass: resolve CALLS precisely via scip-typescript's SCIP index, layered over the fast pass's
    structural subgraph (same node ids). Returns None (caller falls back to tree-sitter) if scip-typescript
    can't run or analyzed none of our callables. The overlay is SURGICAL -- it only drops a fast name-CALLS
    where deep IDENTIFIED that call (resolved it in-source, or identified it as an external the fast pass
    guessed as a local); a call deep couldn't identify (ambiguous same-line def) keeps its fast edge. On an
    upgrade (fast->deep at the same commit) extern:// placeholders are left alone (diff_extern_edges=False),
    so cross-repo reconciliation isn't disturbed."""
    try:
        from auryn.core.scip_resolve import resolve_calls
    except ImportError:  # a plugin-only upgrade left an older auryn-core without the resolver
        logger.warning("typescript deep pass: this auryn-core lacks core.scip_resolve (upgrade auryn-core); "
                       "falling back to tree-sitter")
        return None
    from auryn.ingestors.typescript import scip_typescript

    def moved() -> "str | None":
        # the graph moved off the revision we started from (a concurrent ingest) -> our result is stale.
        # Compare against ENTRY, not target: a direct A->B deep sync legitimately still reads A here.
        live = writer.get_property("Project", {"id": proj_id}, "commit_sha")
        return str(live) if live is not None and live != entry_sha else None

    def _fall_back() -> "SyncStats | None":
        # Deep can't proceed. If a concurrent ingest moved the commit, no-op (don't roll it back). On a
        # same-commit UPGRADE the fast graph already exists at this commit, so return a no-change result
        # rather than re-emitting it; a standalone deep sync at a NEW commit returns None to fall through
        # and build the fast graph via the tree-sitter pass.
        live = moved()
        if live is not None or is_upgrade:
            return SyncStats(commit_sha=live or target_sha, elapsed_seconds=time.monotonic() - started)
        return None

    try:
        # scip-typescript needs an on-disk checkout. A git source yields an immutable checkout at
        # target_sha; a LOCAL directory yields the live tree (as the fast parse also reads it), so an edit
        # landing between the parse and this SCIP read can skew the join -- worst case a few calls fall
        # back to fast. A content snapshot would close that live-dir window (v2).
        with source.working_tree(target_sha) as workdir:
            scip_bytes = scip_typescript.run_scip(str(workdir))
    except scip_typescript.ScipTypescriptUnavailable as exc:
        logger.warning("typescript deep pass unavailable (%s); falling back to tree-sitter", exc)
        return _fall_back()

    locations = resolution.callable_locations(
        (_stamped(load_fn(sha), path) for path, sha in file_refs), proj_id)
    # require_call_site: a SCIP reference to a function isn't always a call -- `return helper` /
    # `callback(helper)` / `typeof helper` use it as a VALUE. Gating on the exact line where the fast parse
    # recorded a call of that name (not just the name, anywhere in the body) keeps deep from inventing CALLS
    # edges for those (JS/TS lean on higher-order functions), including a value use of a name that IS called
    # on another line. The fast parse records every syntactic call site with its callee-identifier line.
    # (An aliased call -- `import {helper as h}; h()`, where SCIP's name `helper` differs from the fast name
    # `h` -- needs occurrence columns to resolve; it's left to the fast pass for now. See #802.)
    # emit_self_calls: the TS fast pass emits self-CALLS, so deep must too -- it precisely replaces a fast
    # recursive edge, captures recursion even when a duplicate-named fast guess was dropped, and lets the
    # overlay drop a FALSE self-guess (e.g. `xs.map()` guessed as the enclosing `map`) that SCIP resolved
    # to an external callee.
    deep = resolve_calls(scip_bytes, locations, require_call_site=True, emit_self_calls=True)
    if locations and not deep.covered_ids:
        # scip-typescript ran but analyzed none of our callables (no tsconfig/deps, unsupported project):
        # don't make a hollow depth="deep" claim -- fall back to the fast graph.
        logger.warning("typescript deep pass: scip-typescript covered no callables; falling back to tree-sitter")
        return _fall_back()

    loc_by_id = {loc.id: loc for loc in locations}
    # Keep the project node at "fast" in the delta; depth is bumped to "deep" by an ATOMIC guarded write
    # AFTER the delta lands, so a concurrent ingest that moved the commit can't be overwritten with a stale
    # "deep" claim.
    project_node = _project_node(proj_id, project_name, repo_uri, target_sha, ref, "fast")

    def make_target():  # a GENERATOR -- preserves emit_streaming's bounded memory
        yield project_node
        for item in resolution.emit_streaming(plan, load_fn, proj_id):
            if _is_name_calls_edge(item):
                frm = item.from_key["id"]
                callee = loc_by_id.get(item.to_key["id"])
                # drop a fast name-edge where deep identified that intra-project call (a resolved deep edge
                # replaces it -- including a self/recursive edge, since emit_self_calls makes deep author
                # those). Extern/other-repo callees (callee is None -> not a local callable) are kept.
                if (frm in deep.covered_ids and callee is not None
                        and callee.name in deep.seen_by_caller.get(frm, set())):
                    continue
            yield item
        yield from deep.edges

    live = moved()  # freshness guard before the write: discard a stale revision rather than clobber a newer one
    if live is not None:
        logger.warning("typescript deep pass: graph moved to %s while analyzing %s; discarding stale result",
                       live, target_sha)
        return SyncStats(commit_sha=live, elapsed_seconds=time.monotonic() - started)

    result = _apply(writer, proj_id, make_target, target_sha, started, files_parsed=files_parsed,
                    files_cached=files_cached, broken=plan.broken, diff_extern_edges=not is_upgrade)
    # Atomically stamp depth="deep" only if the graph is STILL at this commit (GraphWriter.set_if), so the
    # depth marker never makes a stale deep claim if a concurrent ingest moved the commit while we deepened.
    writer.set_if("Project", proj_id, "depth", "deep", "commit_sha", target_sha)
    logger.info("typescript deep: %d resolved / %d external CALLS over %d/%d callables covered",
                deep.metrics["resolved"], deep.metrics["external"], len(deep.covered_ids), len(locations))
    return _finish(writer, proj_id, source, result)


def _route(writer, source, target_sha, project_name, repo_uri, ref, current_sha) -> SyncStats | None:
    """Attempt an incremental resync via the shared engine's git routing; return SyncStats or
    None to fall back to the full recompute (which leaves the graph untouched)."""
    from auryn.core.incremental_scoped import route_incremental
    from auryn.ingestors.typescript.incremental import TS_SPEC

    s = route_incremental(
        writer, TS_SPEC, source, target_sha, ref, current_sha,
        project_name=project_name, repo_uri=repo_uri, logger=logger,
    )
    if s is None:
        return None
    return SyncStats(
        nodes_added=s.nodes_added, nodes_updated=s.nodes_updated, nodes_removed=s.nodes_removed,
        edges_added=s.edges_added, edges_updated=s.edges_updated, edges_removed=s.edges_removed,
        commit_sha=s.commit_sha, elapsed_seconds=s.elapsed_seconds,
        files_parsed=s.files_parsed, files_cached=s.files_cached, broken_files=s.broken_files,
    )


def _finish(writer, proj_id: str, source, result: SyncStats) -> SyncStats:
    """After any non-short-circuit sync, record the working-tree base for the next
    save-triggered incremental resync (a no-op for git/plain-dir sources)."""
    from auryn.core.incremental_scoped import stamp_worktree
    from auryn.core.local_source import WorkingTreeSource

    if isinstance(source, WorkingTreeSource):
        stamp_worktree(writer, proj_id, source, _FILE_SUFFIXES)
    return result


def _apply(writer, proj_id: str, make_target, target_sha: str, started: float,
           files_parsed: int, files_cached: int = 0, broken: list[str] | None = None,
           *, diff_extern_edges: bool = True) -> SyncStats:
    # Cross-repo reconciliation (ADR 0004); the incremental path gets it via incremental_scoped.
    from auryn.core.linkage import reconcile_linkages

    # stream_apply writes the delta without materialising the target graph, and returns the
    # linkage strings collected inline (see graph_sync.stream_apply / core.linkage). The deep pass
    # passes diff_extern_edges=False on an upgrade so it doesn't prune the fast pass's extern://
    # placeholder CALLS (it can't author cross-repo edges; reconcile_linkages heals them later).
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
        files_cached=files_cached,
        broken_files=broken or [],
    )


# -- blob-sha parse cache + streaming parse (memory-bounded first ingest) ------------------


def _cache_path(parse_cache_dir: Path, blob_sha: str) -> Path:
    return parse_cache_dir / f"{blob_sha}.pickle"


def _load_cached(parse_cache_dir: Path, blob_sha: str) -> RawFile | None:
    path = _cache_path(parse_cache_dir, blob_sha)
    if not path.exists():
        return None
    try:
        with path.open("rb") as f:
            obj = pickle.load(f)
    except Exception:
        return None  # corrupt / foreign pickle -- treat as a miss and re-parse
    return obj if isinstance(obj, RawFile) else None


def _store_cached(parse_cache_dir: Path, blob_sha: str, raw_file: RawFile) -> None:
    parse_cache_dir.mkdir(parents=True, exist_ok=True)
    with _cache_path(parse_cache_dir, blob_sha).open("wb") as f:
        pickle.dump(raw_file, f)


def _ensure_all_cached(
    source, target_sha: str, parse_cache_dir: Path
) -> tuple[list[tuple[str, str]], int, int]:
    """Guarantee every JS/TS file at `target_sha` is in the blob-sha parse cache, then return
    its (path, blob_sha) refs -- NOT the parsed objects. Uncached files are parsed across a process
    pool and each RawFile is written to disk and dropped as its parse completes (via the shared
    parse fan-out's `sink`), so a large first ingest never holds every RawFile at once. Files already
    parsed under any other path/commit are reused (a rename is a cache hit). Returns (refs, parsed,
    cached)."""
    entries = [
        (path, blob_sha)
        for suffix in _FILE_SUFFIXES
        for path, blob_sha in source.list_files(target_sha, suffix, set(_EXCLUDED_DIR_NAMES))
    ]
    to_parse = [(p, b) for p, b in entries if not _cache_path(parse_cache_dir, b).exists()]
    parse_blobs_ordered(
        to_parse, source.read_blob, parse_source,
        preload=["auryn.ingestors.typescript.file_parser"], logger=logger,
        sink=lambda entry, raw_file: _store_cached(parse_cache_dir, entry[1], raw_file),
    )
    return entries, len(to_parse), len(entries) - len(to_parse)


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
    """GitLanguage adapter for the multi-language endpoint (TS has no solution knob -- accepted
    for the shared interface and ignored; cache_root is used for the blob-sha parse cache).
    `engine` selects the fast tree-sitter or deep scip-typescript pass."""
    s = sync_at(writer, source, target_sha, project_name=project_name, repo_uri=repo_uri, ref=ref,
                cache_root=cache_root, engine=engine)
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
        broken_files=s.broken_files,
    )


def treesitter_language_sync(writer, source, target_sha, **kwargs) -> LanguageIngestResult:
    """Fast-phase GitLanguage `sync` thunk (the default engine)."""
    return _git_language_sync(writer, source, target_sha, engine="treesitter", **kwargs)


def scip_typescript_language_sync(writer, source, target_sha, **kwargs) -> LanguageIngestResult:
    """Deep-phase GitLanguage `deep_sync` thunk (scip-typescript / SCIP, degrading to the fast pass)."""
    return _git_language_sync(writer, source, target_sha, engine="scip_typescript", **kwargs)


# The GitLanguage is registered by the import-light plugin.py (the entry-point target), whose lazy
# `sync`/`deep_sync` thunks call the adapters above -- so importing this tree-sitter engine module
# (and, for the deep phase, scip-typescript) is deferred until a source actually contains JS/TS files.
