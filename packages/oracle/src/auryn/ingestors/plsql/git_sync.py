"""Git-tracked PL/SQL ingestion: moves a file-based PL/SQL project's graph to a
requested commit/branch/tag by recomputing the target state and diffing it against
the graph -- the same full-recompute-and-diff mechanism the C# git ingestor uses (see
ingestors/csharp/git_sync.py for the design rationale; PL/SQL call resolution is
likewise global, so a per-file source diff can't tell which untouched files a change
forces re-examining).

Registered as the `plsql` GitLanguage so the multi-language ingestion endpoint
(core/ingest.py) picks it up, and exposed directly as `auryn git-sync plsql`.

Memory-bounded end to end (so a large first ingest doesn't hold the whole corpus's
source): _ensure_all_cached parses files straight into the blob-sha cache and drops
them; resolution.build_plan then reads that cache once into a lightweight index and
resolution.emit_streaming emits per entity, loading only the file(s) each needs; and
graph_sync.stream_apply writes the diff without materialising the target graph. This
module adds the git blob-cache + streaming-parse plumbing on top of those.
"""

from __future__ import annotations

import itertools
import logging
import os
import pickle
import time
from concurrent.futures import FIRST_COMPLETED, wait
from dataclasses import dataclass
from pathlib import Path

from auryn.core import progress
from auryn.core.git_source import GitRepoCache, cache_home, repos_cache_dir
from auryn.core.graph_client import GraphWriter
from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.core.graph_sync import stream_apply
from auryn.core.registry import LanguageIngestResult
from auryn.ingestors.plsql import resolution
from auryn.ingestors.plsql.config import PLSQL_FILE_SUFFIXES, GitPLSQLProjectConfig, project_id
from auryn.ingestors.plsql.raw_model import RawPlsqlFile
from auryn.ingestors.plsql.schema import FILE_NODE_SCHEMAS
def _sa():
    """Lazily import source_analysis (pulls the generated ANTLR grammar, ~160ms) so importing this
    module doesn't load the grammar until a parse actually runs. Cached after first import."""
    import auryn.ingestors.plsql.source_analysis as sa

    return sa

logger = logging.getLogger(__name__)

_EXCLUDED_DIR_NAMES: frozenset[str] = frozenset()

_PARSE_CACHE_VERSION = 2
"""Bump whenever RawPlsqlFile's shape (or what parse_declarations extracts) changes.
The blob-sha parse cache stores pickled RawPlsqlFile objects; a pickle written by an
older shape would unpickle into the new class missing fields and blow up at use. The
version is part of the cache directory name, so a bump simply starts a fresh cache
(old dirs are ignored, harmless) rather than crashing on a stale entry -- and
_load_cached additionally treats any unreadable/foreign pickle as a miss."""


def _parse_workers() -> int:
    """Parse-pool size. Default: CPU count. `AURYN_PARSE_WORKERS` caps it -- lower it on a
    memory-constrained box: each parse worker holds its own ANTLR prediction cache (recycled
    every N files, but still per-worker), so peak parse memory scales with the worker count."""
    env = os.environ.get("AURYN_PARSE_WORKERS", "").strip()
    if env.isdigit() and int(env) > 0:
        return int(env)
    return os.cpu_count() or 1


def _max_file_bytes() -> int:
    """Byte size above which a file is skipped rather than parsed (0 = no limit, the default).
    Set `AURYN_MAX_FILE_KB` to bound it: a single pathological PL/SQL file (a huge generated
    package) can cost several GB to ANTLR-parse regardless of worker count -- capping it trades
    that file's subgraph for a bounded peak. Skipped files are logged and cached as empty."""
    kb = os.environ.get("AURYN_MAX_FILE_KB", "").strip()
    return int(kb) * 1024 if kb.isdigit() and int(kb) > 0 else 0


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


def sync(
    writer: GraphWriter,
    config: GitPLSQLProjectConfig,
    ref: str | None,
    cache_root: Path | None = None,
) -> SyncStats:
    """Ingest (first run) or move (every run after) a git-tracked PL/SQL project's
    graph to `ref`. Fetches, resolves `ref` to a commit, then delegates to
    sync_at()."""
    repo = GitRepoCache(config.repo_uri, repos_cache_dir(cache_root))
    repo.fetch()
    target_sha = repo.resolve_ref(ref)
    return sync_at(writer, config, repo, target_sha, ref, cache_root)


def sync_at(
    writer: GraphWriter,
    config: GitPLSQLProjectConfig,
    repo: GitRepoCache,
    target_sha: str,
    ref: str | None,
    cache_root: Path | None = None,
) -> SyncStats:
    """The per-commit half of sync(), given an already-fetched `repo` and resolved
    `target_sha` -- lets the multi-language endpoint fetch once and drive several
    languages against the same commit."""
    started = time.monotonic()
    proj_id = project_id(config.project_name, config.solution_name)

    writer.setup_schema(FILE_NODE_SCHEMAS)  # brings the graph key into existence before any read

    current_sha = writer.get_property("Project", {"id": proj_id}, "commit_sha")
    if current_sha == target_sha:
        return SyncStats(commit_sha=target_sha, elapsed_seconds=time.monotonic() - started)

    workers = config.workers or _parse_workers()
    parse_cache_dir = (cache_root or cache_home()) / f"parse_cache_plsql.v{_PARSE_CACHE_VERSION}"

    # Parse-and-cache every file (streaming: parsed files are cached and dropped, never
    # accumulated in one list), then resolve from the cache one entity at a time. This is
    # what keeps a large first ingest from holding the whole corpus's source in memory --
    # see resolution.build_plan / emit_streaming.
    file_refs, files_parsed, files_cached = _ensure_all_cached(repo, target_sha, parse_cache_dir, workers)

    def load_fn(blob_sha: str) -> RawPlsqlFile:
        # A FRESH RawPlsqlFile every call: resolution._apply_alterations mutates in place,
        # so a shared object would accumulate ALTER columns across reloads. _ensure_all_cached
        # guaranteed presence; a miss here means a corrupt/evicted entry -- re-parse it.
        cached = _load_cached(parse_cache_dir, blob_sha)
        if cached is None:
            cached = _parse_blob(("", repo.read_blob(blob_sha)))
            _store_cached(parse_cache_dir, blob_sha, cached)
        return cached

    plan = resolution.build_plan(file_refs, load_fn, proj_id)  # phase 1: lightweight index only

    # Prelude nodes/edges this ingestor owns directly (the resolution layer emits
    # everything else). A plain list so make_target() below can re-iterate it each pass.
    prelude: list[GraphNode | GraphEdge] = [
        GraphNode(
            "Project",
            {"id": proj_id},
            {
                "name": config.project_name,
                "solution_name": config.solution_name,
                "repo_uri": config.repo_uri,
                "commit_sha": target_sha,
                "ref": ref,
                "provenance": "source",
            },
        )
    ]
    if config.solution_name:
        solution_id = f"plsql://{config.solution_name}"
        prelude.append(GraphNode("Solution", {"id": solution_id}, {"name": config.solution_name}))
        prelude.append(GraphEdge("HAS_PROJECT", "Solution", {"id": solution_id}, "Project", {"id": proj_id}))

    # stream_apply calls this once for the nodes pass and once for the edges pass, so it
    # must return a fresh, equivalent iterator each time -- emit_streaming re-loads per
    # entity from the cache each call, so nothing is retained between passes.
    def make_target():
        return itertools.chain(prelude, resolution.emit_streaming(plan, load_fn, proj_id))

    labels = [schema.label for schema in FILE_NODE_SCHEMAS]
    # PL/SQL ids continue the project id with "." (`<proj>.package.<name>`), not "/".
    stats, _reals, _placeholders = stream_apply(writer, proj_id, labels, make_target, id_sep=".")
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
    )


def _parse_blob(entry: tuple[str, bytes]) -> RawPlsqlFile:
    path, source = entry
    return _sa().parse_declarations(source.decode("utf-8", errors="replace"), path)


def _cache_path(parse_cache_dir: Path, blob_sha: str) -> Path:
    return parse_cache_dir / f"{blob_sha}.pickle"


def _load_cached(parse_cache_dir: Path, blob_sha: str) -> RawPlsqlFile | None:
    path = _cache_path(parse_cache_dir, blob_sha)
    if not path.exists():
        return None
    try:
        with path.open("rb") as f:
            obj = pickle.load(f)
    except Exception:
        return None  # corrupt / foreign pickle -- treat as a miss and re-parse
    return obj if isinstance(obj, RawPlsqlFile) else None


def _store_cached(parse_cache_dir: Path, blob_sha: str, raw_file: RawPlsqlFile) -> None:
    parse_cache_dir.mkdir(parents=True, exist_ok=True)
    with _cache_path(parse_cache_dir, blob_sha).open("wb") as f:
        pickle.dump(raw_file, f)


def _list_files(repo: GitRepoCache, sha: str) -> list[tuple[str, str]]:
    """Every PL/SQL-suffixed blob at `sha` -- (path, blob_sha) pairs across all
    supported extensions (each path matches exactly one, so no dedup needed)."""
    entries: list[tuple[str, str]] = []
    for suffix in PLSQL_FILE_SUFFIXES:
        entries.extend(repo.list_files(sha, suffix, set(_EXCLUDED_DIR_NAMES)))
    return entries


def _ensure_all_cached(
    repo: GitRepoCache, sha: str, parse_cache_dir: Path, workers: int
) -> tuple[list[tuple[str, str]], int, int]:
    """Guarantee every PL/SQL file at `sha` is present in the blob-sha parse cache, then
    return its (path, blob_sha) references -- NOT the parsed objects. Files already parsed
    under any other path/commit are reused (a pure rename is a cache hit). Unlike the old
    _build_raw_files, parsed files are streamed straight to disk and dropped rather than
    collected into one list, so a large first ingest never holds the whole corpus in
    memory (the whole point of streaming resolution). Returns (refs, files_parsed,
    files_cached)."""
    entries = _list_files(repo, sha)
    # Cheap existence check (no unpickle) -- a corrupt/foreign cache file that slips through
    # here is re-parsed on demand by sync_at's load_fn, so this stays O(1) per file.
    to_parse = [(path, blob_sha) for path, blob_sha in entries if not _cache_path(parse_cache_dir, blob_sha).exists()]
    files_cached = len(entries) - len(to_parse)
    if to_parse:
        engine = "C++ accelerator 'plsql_accel' (fast path)" if _sa().accelerator_available() else (
            "pure-Python ANTLR ('plsql_accel' not installed -- expect ~10x slower)"
        )
        logger.info("PL/SQL parser engine: %s (%d file(s) to parse, %d cached)", engine, len(to_parse), files_cached)
        _parse_into_cache(repo, to_parse, parse_cache_dir, workers)
    return entries, len(to_parse), files_cached


def _parse_into_cache(
    repo: GitRepoCache, to_parse: list[tuple[str, str]], parse_cache_dir: Path, workers: int
) -> None:
    """Parse each (path, blob_sha) and write the result to the cache, holding at most a
    few parsed files at once. Blob bytes are read on demand (not all up front) and each
    result is stored-and-dropped as it completes, so parent-process memory stays bounded
    regardless of corpus size."""
    parse_cache_dir.mkdir(parents=True, exist_ok=True)
    verbose = progress.verbose_enabled()
    max_bytes = _max_file_bytes()
    total = len(to_parse)

    def _too_big(path: str, source: bytes) -> bool:
        """A file over the cap is skipped (cached empty) -- one monster file can cost GBs to
        parse no matter the worker count, so this bounds peak parse memory."""
        if max_bytes and len(source) > max_bytes:
            logger.warning("Skipping %s: %.1f MB exceeds AURYN_MAX_FILE_KB cap -- cached empty",
                           path, len(source) / 1e6)
            return True
        return False

    if workers == 1 or total <= 1:
        # Sequential -- reachable only via `--workers 1` (crash-pinpoint: verbose logs each file
        # *before* parsing it, so a native segfault leaves the offender as the last log line).
        # WARNING: parses in THIS process with no worker recycling, so ANTLR's prediction cache
        # grows unbounded over a large file set -- use it to isolate a crash, not for a big repo.
        for i, (path, blob_sha) in enumerate(to_parse, 1):
            if verbose:
                progress.log_file(logger, path, i, total)
            source = repo.read_blob(blob_sha)
            result = RawPlsqlFile(path=path) if _too_big(path, source) else _parse_blob((path, source))
            _store_cached(parse_cache_dir, blob_sha, result)
        return

    # Parallel with a bounded window (~2 tasks/worker in flight, cache+drop each result as it
    # lands). RECYCLING workers (via file_ingestor._make_pool: forkserver + max_tasks_per_child)
    # is what keeps ANTLR's per-worker prediction cache from growing unbounded across a large
    # corpus -- without it a long-lived worker OOMs a few hundred files in. verbose logs each
    # file as it COMPLETES, so progress and all cores coexist (unlike the old verbose->sequential).
    from auryn.ingestors.plsql.file_ingestor import _make_pool

    pending = iter(to_parse)
    done_count = 0
    with _make_pool(workers) as pool:
        inflight: dict = {}

        def submit_next() -> bool:
            for path, blob_sha in pending:  # skip over-cap files in the parent (never dispatched)
                source = repo.read_blob(blob_sha)
                if _too_big(path, source):
                    _store_cached(parse_cache_dir, blob_sha, RawPlsqlFile(path=path))
                    continue
                inflight[pool.submit(_parse_blob, (path, source))] = (path, blob_sha)
                return True
            return False

        for _ in range(workers * 2):
            if not submit_next():
                break
        while inflight:
            done, _running = wait(inflight, return_when=FIRST_COMPLETED)
            for future in done:
                path, blob_sha = inflight.pop(future)
                _store_cached(parse_cache_dir, blob_sha, future.result())
                done_count += 1
                if verbose:
                    progress.log_file(logger, path, done_count, total)
                submit_next()


def _git_language_sync(
    writer: GraphWriter,
    repo: GitRepoCache,
    target_sha: str,
    *,
    repo_uri: str,
    project_name: str,
    solution_name: str | None,
    ref: str | None,
    cache_root: Path | None = None,
) -> LanguageIngestResult:
    """GitLanguage adapter for the multi-language ingestion endpoint -- see the C#
    equivalent in ingestors/csharp/git_sync.py."""
    config = GitPLSQLProjectConfig(repo_uri=repo_uri, project_name=project_name, solution_name=solution_name)
    s = sync_at(writer, config, repo, target_sha, ref, cache_root)
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


# The GitLanguage is registered by the import-light plugin.py (the entry-point target),
# whose lazy `sync` thunk calls `_git_language_sync` above -- so importing this heavy engine
# module (the ANTLR grammar) is deferred until a source actually has PL/SQL files.
