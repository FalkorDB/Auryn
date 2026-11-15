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

_PARSE_CACHE_VERSION = 2
"""Bump if RawFile's shape (or what parse_source extracts) changes. Part of the cache dir
name, so a bump starts a fresh cache rather than unpickling a stale shape. v2: callable/type
dataclasses gained the shared `doc` property (leading comment)."""

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


def sync_at(writer, source, target_sha: str, project_name: str, repo_uri: str, ref: str | None,
            cache_root: Path | None = None) -> SyncStats:
    """Move the whole JS/TS subgraph to `target_sha`. Takes the incremental path (ADR 0002)
    when a prior graph exists at a different state and git can name the change; otherwise a
    full recompute + delta. `source` is duck-typed (GitRepoCache / LocalDirectorySource /
    WorkingTreeSource).

    The full-recompute path is memory-bounded: files are parsed straight into the blob-sha
    cache and dropped (never all held at once), then resolution.build_plan reads that cache
    into a lightweight scope and resolution.emit_streaming emits one file at a time -- so a
    large first ingest doesn't hold every file's declaration source in memory."""
    started = time.monotonic()
    proj_id = project_id(project_name)

    writer.setup_schema(NODE_SCHEMAS)  # brings the graph key into existence before the first read
    current_sha = writer.get_property("Project", {"id": proj_id}, "commit_sha")
    if current_sha == target_sha:
        return SyncStats(commit_sha=target_sha, elapsed_seconds=time.monotonic() - started)

    if current_sha:  # a prior graph exists -> try to move it incrementally (single-phase, no depth marker)
        incr = _route(writer, source, target_sha, project_name, repo_uri, ref, current_sha)
        if incr is not None:
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
    project_node = GraphNode(
        "Project", {"id": proj_id}, {"name": project_name, "repo_uri": repo_uri, "commit_sha": target_sha, "ref": ref}
    )

    def make_target():  # re-iterable: stream_apply runs it once for nodes, once for edges
        return itertools.chain([project_node], resolution.emit_streaming(plan, load_fn, proj_id))

    return _finish(writer, proj_id, source,
                   _apply(writer, proj_id, make_target, target_sha, started,
                          files_parsed=files_parsed, files_cached=files_cached, broken=plan.broken))


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
           files_parsed: int, files_cached: int = 0, broken: list[str] | None = None) -> SyncStats:
    # Cross-repo reconciliation (ADR 0004); the incremental path gets it via incremental_scoped.
    from auryn.core.linkage import reconcile_linkages

    # stream_apply writes the delta without materialising the target graph, and returns the
    # linkage strings collected inline (see graph_sync.stream_apply / core.linkage).
    labels = [s.label for s in NODE_SCHEMAS]
    stats, reals, placeholders = stream_apply(writer, proj_id, labels, make_target)
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
) -> LanguageIngestResult:
    """GitLanguage adapter for the multi-language endpoint (TS has no solution knob -- accepted
    for the shared interface and ignored; cache_root is used for the blob-sha parse cache)."""
    s = sync_at(writer, source, target_sha, project_name=project_name, repo_uri=repo_uri, ref=ref,
                cache_root=cache_root)
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


# The GitLanguage is registered by the import-light plugin.py (the entry-point target), whose
# lazy `sync` thunk calls `_git_language_sync` above -- so importing this tree-sitter engine
# module is deferred until a source actually contains JS/TS files.
