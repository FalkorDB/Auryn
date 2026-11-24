"""Git-tracked (and directory) Python ingestion: moves a project's Python subgraph to a
target commit/tree by fully recomputing the target state and diffing it against the graph
(graph_sync.compute_delta) -- the same recompute-and-delta model as the C# ingestor, and
for the same reason (resolution is global, so a source-level file diff can't tell which
untouched files need re-examining).

Two in-process engines, selected like C#'s roslyn/treesitter:
  * "jedi" (default) -- layers Jedi's static inference over the ast structural pass:
    inferred variable/attribute/param types and resolved calls, including receiver-typed
    ones (`conn.execute()`). Jedi is a pure-Python pip dependency; if it's somehow not
    importable we fall back to "ast" automatically.
  * "ast" -- the stdlib-only engine (file_parser + resolution with no oracle): syntax
    backbone plus the inheritance/calls it can statically resolve. Zero extra deps.

Registered as a GitLanguage for ".py", so the multi-language endpoint (core/ingest.py)
owns Python files in both ingest_repository (git) and ingest_directory (local).
"""

from __future__ import annotations

import itertools
import logging
import os
import time
from concurrent.futures import FIRST_COMPLETED, wait
from dataclasses import dataclass, field
from pathlib import Path

from auryn.core import progress
from auryn.core.git_source import GitRepoCache, repos_cache_dir
from auryn.core.parallel import make_process_pool
from auryn.core.graph_client import GraphWriter
from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.core.graph_sync import stream_apply
from auryn.core.instrument import phase
from auryn.core.registry import LanguageIngestResult
from auryn.ingestors.python import resolution
from auryn.ingestors.python.file_parser import parse_blob, parse_module
from auryn.ingestors.python.ids import package_dirs_from_paths as ids_pkg_dirs
from auryn.ingestors.python.ids import project_id
from auryn.ingestors.python.plugin import EXCLUDED_DIR_NAMES as _EXCLUDED_DIR_NAMES
from auryn.ingestors.python.schema import NODE_SCHEMAS

logger = logging.getLogger(__name__)

DEFAULT_ENGINE = "jedi"
"""Extraction engine for git-sync and the ingestion endpoint. "jedi" adds inferred types +
resolved calls in-process; it falls back to "ast" (zero-dep, stdlib) automatically if Jedi
isn't importable."""

# How deep a graph the two engines produce, for the two-phase short-circuit in sync_at:
# ast = "fast", jedi = "deep". A graph already at >= the requested depth (same commit) is
# left untouched. Unknown/missing (a pre-two-phase graph, or None) ranks lowest.
_DEPTH_RANK = {None: 0, "": 0, "fast": 1, "deep": 2}

# _FILE_SUFFIXES / _EXCLUDED_DIR_NAMES are defined once in plugin.py (the import-light
# registration module) and imported above, so the language's suffixes have a single source
# of truth shared by the lazy GitLanguage registration and the walkers below.


@dataclass(frozen=True)
class GitPythonProjectConfig:
    repo_uri: str
    project_name: str
    workers: int | None = None
    """Process-pool size for the jedi engine's per-file inference. None = os.cpu_count()."""


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


def sync(
    writer: GraphWriter,
    config: GitPythonProjectConfig,
    ref: str | None,
    cache_root: Path | None = None,
    engine: str = DEFAULT_ENGINE,
) -> SyncStats:
    """Ingest (first run) or move (every run after) a git-tracked Python project's graph
    to `ref`. Fetches, resolves `ref` to a commit, then delegates to sync_at()."""
    repo = GitRepoCache(config.repo_uri, repos_cache_dir(cache_root))
    repo.fetch()
    target_sha = repo.resolve_ref(ref)
    return sync_at(writer, config, repo, target_sha, ref, engine)


def sync_at(
    writer: GraphWriter,
    config: GitPythonProjectConfig,
    source,
    target_sha: str,
    ref: str | None,
    engine: str = DEFAULT_ENGINE,
) -> SyncStats:
    """The per-commit half of sync(), given an already-prepared `source` (a GitRepoCache
    or a duck-typed LocalDirectorySource) and a resolved `target_sha`."""
    started = time.monotonic()
    proj_id = project_id(config.project_name)

    # Creating the indexes brings the graph key into existence before the first read
    # (FalkorDB's RO_QUERY rejects an empty key) -- same ordering C# relies on.
    writer.setup_schema(NODE_SCHEMAS)

    # Depth marker for two-phase ingestion: the fast (ast) pass and the deep (jedi) pass
    # both target the same commit, so a bare commit_sha check would let the fast pass'
    # recorded sha short-circuit the deep upgrade. We record how deep the graph already is
    # ("fast" < "deep") and skip only when the graph is at least as deep as this engine
    # would make it -- so the deep pass still runs right after the fast one, yet a genuine
    # re-ingest of an unchanged, already-deep project is still a no-op.
    current_sha = writer.get_property("Project", {"id": proj_id}, "commit_sha")
    current_depth = writer.get_property("Project", {"id": proj_id}, "depth")
    requested_depth = "deep" if engine == "jedi" else "fast"
    if current_sha == target_sha and _DEPTH_RANK.get(current_depth, 0) >= _DEPTH_RANK[requested_depth]:
        return SyncStats(commit_sha=target_sha, elapsed_seconds=time.monotonic() - started)

    # Incremental resync (ADR 0002): when a prior ast graph already exists at a *reachable*
    # git commit, move it by processing only the git diff + its graph-derived blast radius,
    # instead of recomputing the whole tree. Guarded tightly -- ast engine, git source, an
    # existing fast-depth graph at a different commit -- and any failure (e.g. the old commit
    # is unreachable in a shallow clone) falls straight back to the full recompute below.
    if engine == "ast" and current_sha and current_sha != target_sha and current_depth == "fast":
        incr = _route(writer, config, source, target_sha, ref, current_sha)
        if incr is not None:
            return _finish(writer, proj_id, source, incr)

    if engine == "jedi":
        import importlib.util

        if importlib.util.find_spec("jedi") is None:
            logger.warning("jedi is not installed; falling back to the ast engine")
        else:
            # Is this a genuine same-commit fast->deep UPGRADE (the ast pass just ran at this commit)?
            # Only then does jedi preserve the ast pass's extern:// placeholders (it can't resolve
            # cross-repo and so doesn't re-author them). A standalone jedi resync (no preceding ast pass,
            # e.g. a different commit) is authoritative instead, so it must still diff stale extern edges
            # away -- otherwise a dropped unresolved call leaves a stale placeholder (PR #17 review).
            is_upgrade = current_sha == target_sha and current_depth == "fast"
            result = _jedi_sync_at(writer, config, source, target_sha, ref, proj_id, started,
                                   diff_extern_edges=not is_upgrade)
            return _finish(writer, proj_id, source, result)
    return _finish(writer, proj_id, source, _ast_sync_at(writer, config, source, target_sha, ref, proj_id, started))


def _route(writer, config, source, target_sha, ref, current_sha) -> SyncStats | None:
    """Attempt an incremental resync via the shared engine's git routing (working-tree or
    bare-clone producer, ADR 0002); return SyncStats or None to fall back to full recompute."""
    from auryn.core.incremental_scoped import route_incremental
    from auryn.ingestors.python.incremental import PYTHON_SPEC

    s = route_incremental(
        writer, PYTHON_SPEC, source, target_sha, ref, current_sha,
        project_name=config.project_name, repo_uri=config.repo_uri, logger=logger,
    )
    if s is None:
        return None
    return SyncStats(
        nodes_added=s.nodes_added, nodes_updated=s.nodes_updated, nodes_removed=s.nodes_removed,
        edges_added=s.edges_added, edges_updated=s.edges_updated, edges_removed=s.edges_removed,
        commit_sha=s.commit_sha, elapsed_seconds=s.elapsed_seconds,
        files_parsed=s.files_parsed, files_cached=s.files_cached, broken_files=s.broken_files,
    )


def _finish(writer: GraphWriter, proj_id: str, source, result: SyncStats) -> SyncStats:
    """After any non-short-circuit sync, record the working-tree base for the next
    save-triggered incremental resync (a no-op for git/plain-dir sources)."""
    from auryn.core.incremental_scoped import stamp_worktree
    from auryn.core.local_source import WorkingTreeSource

    if isinstance(source, WorkingTreeSource):
        stamp_worktree(writer, proj_id, source, (".py",))
    return result


def _project_items(
    config: GitPythonProjectConfig, proj_id: str, target_sha: str, ref: str | None, depth: str
) -> list[GraphNode | GraphEdge]:
    return [
        GraphNode(
            "Project",
            {"id": proj_id},
            {
                "name": config.project_name,
                "repo_uri": config.repo_uri,
                "commit_sha": target_sha,
                "ref": ref,
                "depth": depth,  # "fast" (ast) or "deep" (jedi) -- see the sync_at short-circuit
            },
        )
    ]


# Below this many files, parse+resolve in memory: parsing is CPU-bound and was the ingest's tall
# pole, so parse once (in parallel) beats the streaming path's re-parse-per-emit (up to 3x/file).
# Above it, fall back to the memory-bounded serial stream so a very large first ingest keeps peak
# RSS flat (holding every module's source at once would dominate). Env-tunable for tuning/tests.
_STREAM_MEMORY_FILES = int(os.environ.get("AURYN_PY_STREAM_FILES", "5000"))
# Below this, skip the process pool -- worker startup (spawn) costs more than it saves on a handful.
_PARALLEL_MIN_FILES = 8
# In-flight parse tasks per worker. Blob bytes are read just-in-time and held only until their task
# completes, so peak transient blob RAM is bounded to ~(workers * this) files -- not the whole tree
# at once (what a plain `pool.map`, which submits every task up front, would hold). >1 keeps each
# worker fed while the previous result is collected.
_INFLIGHT_PER_WORKER = 3


def _parse_modules(entries, source, pkg_dirs, workers: int) -> list:
    """Parse every (path, blob_sha) entry into a RawModule, preserving input order (so ids stay
    deterministic). Serial for a handful of files or workers==1 (pool startup isn't worth it),
    else parallel across a process pool with a bounded in-flight window.

    Memory: a blob is read only when its task is submitted and is released once the task finishes,
    so at most ~(workers * _INFLIGHT_PER_WORKER) source blobs are resident at any moment -- the
    parse parallelizes without holding the whole tree's bytes in memory (which `pool.map` would, by
    submitting every task up front). The parsed RawModules do accumulate -- the in-memory resolve
    pass needs them all -- which is the fast path's deliberate floor (see _ast_sync_at)."""
    entries = list(entries)
    if workers <= 1 or len(entries) <= _PARALLEL_MIN_FILES:
        return [parse_module(source.read_blob(sha), path, pkg_dirs) for path, sha in entries]

    results: list = [None] * len(entries)
    max_inflight = workers * _INFLIGHT_PER_WORKER
    # Preload the parse module (parse_blob's home) into the forkserver control process so workers
    # inherit its import via COW rather than re-importing per worker -- see core.parallel.
    with make_process_pool(workers, preload=["auryn.ingestors.python.file_parser"]) as pool:
        inflight: dict = {}  # Future -> result index
        next_idx = 0

        def submit(i: int) -> None:
            path, sha = entries[i]
            inflight[pool.submit(parse_blob, (path, source.read_blob(sha), pkg_dirs))] = i

        while next_idx < len(entries) and len(inflight) < max_inflight:
            submit(next_idx)
            next_idx += 1
        while inflight:
            done, _ = wait(inflight, return_when=FIRST_COMPLETED)
            for fut in done:
                results[inflight.pop(fut)] = fut.result()  # re-raises a worker error, as pool.map would
                if next_idx < len(entries):
                    submit(next_idx)
                    next_idx += 1
    return results


def _ast_sync_at(writer, config, source, target_sha, ref, proj_id, started) -> SyncStats:
    """Parse `.py` blobs and resolve with no oracle.

    Common path: parse every file **once, in parallel**, then resolve in a single in-memory pass
    (like the jedi engine, minus the oracle). Parsing was the tall pole and the streaming path
    below re-parsed each file up to 3x (once to index, twice to emit for the node+edge passes);
    parsing once across a pool removes both the re-parse and the serialization.

    Fallback (> _STREAM_MEMORY_FILES): the memory-bounded serial stream -- build_plan scans each
    module once into the lightweight index and emit_streaming re-parses one at a time, so a very
    large first ingest never holds every module's source at once. Re-parsing (vs a blob-sha cache)
    keeps each module's relative-import/dotted path correct for the current layout."""
    entries = source.list_files(target_sha, ".py", _EXCLUDED_DIR_NAMES)
    # Package context for import-root detection (`src/` layouts, wrapper dirs): computed once from
    # the whole file list so every module's dotted path is anchored at its true import root (ADR
    # 0004). The incremental engine derives the same context the same way (incremental_scoped).
    pkg_dirs = ids_pkg_dirs([path for path, _ in entries])
    project_items = _project_items(config, proj_id, target_sha, ref, "fast")

    if len(entries) > _STREAM_MEMORY_FILES:
        def load_fn(path, blob_sha):
            return parse_module(source.read_blob(blob_sha), path, pkg_dirs)

        with phase("parse"):
            plan = resolution.build_plan(entries, load_fn, proj_id)

        def make_target():  # re-iterable: stream_apply runs it once for nodes, once for edges
            return itertools.chain(project_items, resolution.emit_streaming(plan, load_fn, proj_id))

        return _apply(writer, proj_id, make_target, target_sha, started,
                      files_parsed=len(entries), broken=plan.broken)

    workers = config.workers or os.cpu_count() or 1
    with phase("parse"):
        modules = _parse_modules(entries, source, pkg_dirs, workers)
    broken = sorted(m.path for m in modules if not m.parse_ok)

    def make_target():  # re-iterable: stream_apply runs it once for nodes, once for edges
        return itertools.chain(project_items, resolution.resolve(modules, proj_id))

    return _apply(writer, proj_id, make_target, target_sha, started,
                  files_parsed=len(modules), broken=broken)


def _jedi_sync_at(writer, config, source, target_sha, ref, proj_id, started, *,
                  diff_extern_edges: bool = True) -> SyncStats:
    """Materialize a working tree (Jedi reads source from disk and needs the whole project
    to resolve cross-file), parse each file, and resolve with a JediOracle over that dir.
    `working_tree` yields a temp checkout for a git source and the directory itself for a
    local source, so this path is identical either way. `diff_extern_edges` is False only for a
    same-commit fast->deep upgrade (preserve the ast pass's cross-repo placeholders); True for a
    standalone jedi resync so a dropped unresolved call's stale placeholder is still removed."""
    from auryn.ingestors.python.jedi_engine import build_precomputed_oracle

    entries = source.list_files(target_sha, ".py", _EXCLUDED_DIR_NAMES)
    relpaths = sorted(path for path, _ in entries)
    with source.working_tree(target_sha) as workdir:
        workdir = Path(workdir)
        for rel, blob_sha in entries:
            path = workdir / rel
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(source.read_blob(blob_sha))
        pkg_dirs = ids_pkg_dirs(relpaths)  # import-root context; see _ast_sync_at
        with phase("parse"):
            modules = []
            for i, rel in enumerate(relpaths, 1):
                progress.log_file(logger, rel, i, len(relpaths))
                modules.append(parse_module((workdir / rel).read_bytes(), rel, pkg_dirs))
        # Fan the (dominant) Jedi inference cost across a process pool up front, inside the
        # `with` -- workers read files that only exist while the working tree is materialized.
        # Keep the project node at "fast" in the delta; depth is bumped to "deep" atomically AFTER the
        # delta lands (guarded on commit_sha), so a concurrent ingest that moved the commit can't be
        # overwritten with a stale deep claim.
        project_items = _project_items(config, proj_id, target_sha, ref, "fast")
        with phase("resolve"):
            oracle = build_precomputed_oracle(str(workdir), modules, workers=config.workers)

        def make_target():  # re-iterable; resolve() re-queries the precomputed oracle each pass
            return itertools.chain(project_items, resolution.resolve(modules, proj_id, oracle=oracle))

        broken = sorted(m.path for m in modules if not m.parse_ok)
        # Stay INSIDE the working-tree context: stream_apply re-runs make_target (hence
        # resolve over the oracle) as it writes, and the oracle is anchored to this checkout.
        result = _apply(writer, proj_id, make_target, target_sha, started, files_parsed=len(modules),
                        broken=broken, diff_extern_edges=diff_extern_edges)
        writer.set_if("Project", proj_id, "depth", "deep", "commit_sha", target_sha)  # atomic depth bump
        return result


def _apply(writer: GraphWriter, proj_id: str, make_target, target_sha: str, started: float,
           files_parsed: int, broken: list[str] | None = None, *, diff_extern_edges: bool = True) -> SyncStats:
    # Cross-repo reconciliation (ADR 0004): fold placeholder<->definer pairs among the linkages
    # this ingest touched -- targeted index seeks, no graph-wide scan. A no-op unless this graph
    # holds more than one repo.
    from auryn.core.linkage import reconcile_linkages

    # stream_apply writes the delta without materialising the target graph, and returns the
    # linkage strings collected inline (see graph_sync.stream_apply / core.linkage). The deep (jedi)
    # pass passes diff_extern_edges=False: it can't resolve cross-repo, so it doesn't re-author the
    # fast pass's extern:// placeholders and must not diff them away before reconcile heals them.
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
        broken_files=broken or [],
    )


def _git_language_sync(
    writer: GraphWriter,
    repo,
    target_sha: str,
    *,
    engine: str,
    repo_uri: str,
    project_name: str,
    solution_name: str | None,
    ref: str | None,
    cache_root: Path | None = None,
) -> LanguageIngestResult:
    """GitLanguage adapter for the multi-language endpoint (Python has no solution/cache
    knobs -- both are accepted for the shared interface and ignored). `engine` picks the
    phase: "ast" (fast) or "jedi" (deep). Both emit identical node ids, so running jedi
    after ast is a pure in-place upgrade."""
    config = GitPythonProjectConfig(repo_uri=repo_uri, project_name=project_name)
    s = sync_at(writer, config, repo, target_sha, ref, engine=engine)
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


def ast_language_sync(writer, repo, target_sha, **kwargs) -> LanguageIngestResult:
    """Fast phase entry point (stdlib `ast`) -- the GitLanguage's `sync`. See plugin.py."""
    return _git_language_sync(writer, repo, target_sha, engine="ast", **kwargs)


def jedi_language_sync(writer, repo, target_sha, **kwargs) -> LanguageIngestResult:
    """Deep phase entry point (Jedi) -- the GitLanguage's `deep_sync`. See plugin.py."""
    return _git_language_sync(writer, repo, target_sha, engine="jedi", **kwargs)


# The GitLanguage itself is registered by the import-light plugin.py (the entry-point
# target), whose lazy `sync`/`deep_sync` thunks call the two adapters above -- so importing
# this heavy engine module is deferred until a source actually contains `.py` files.
