"""Git-tracked C# ingestion: moves a project's graph to whatever commit/branch/tag
is requested -- forward, backward, or across branches -- by fully recomputing the
target state and diffing it against whatever the graph currently holds (see
graph_sync.compute_delta). See the design discussion recorded in this feature's
plan for why a source-level (git diff) approach was rejected: this ingestor's
resolution is global, so a file-level diff can't tell which *other*, untouched
files need re-examining.

Not a BaseIngestor subclass -- its lifecycle (a persistent repo cache, a
`commit_sha` marker on the Project node, real deletions) doesn't fit
BaseIngestor.run()'s stateless "extract a snapshot, merge it in, never delete"
contract.
"""

from __future__ import annotations

import logging
import os
import pickle
import time
from dataclasses import dataclass
from pathlib import Path

from auryn.core import progress
from auryn.core.git_source import DEFAULT_CACHE_ROOT, GitRepoCache, cache_home, repos_cache_dir
from auryn.core.parallel import make_process_pool
from auryn.core.graph_client import GraphWriter
from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.core.graph_sync import stream_apply
from auryn.core.registry import LanguageIngestResult
from auryn.ingestors.csharp import kendo, resolution, roslyn
from auryn.ingestors.csharp.config import project_id
from auryn.ingestors.csharp.file_parser import parse_source
from auryn.ingestors.csharp.plugin import EXCLUDED_DIR_NAMES as _EXCLUDED_DIR_NAMES
from auryn.ingestors.csharp.raw_model import RawFile
from auryn.ingestors.csharp.schema import NODE_SCHEMAS

logger = logging.getLogger(__name__)

DEFAULT_ENGINE = "roslyn"
"""The C# extraction engine used by git-sync and the ingestion endpoint by default.
`roslyn` gives semantic analysis + DI resolution (see roslyn.py); it falls back to
`treesitter` automatically when the .NET SDK / extractor tool isn't available."""

# Two-phase depth marker, uniform with the Python engine (ast=fast < jedi=deep): the fast
# tree-sitter pass and the deep Roslyn pass target the SAME commit, so a bare commit_sha check
# would let the fast pass' sha short-circuit the deep upgrade. We record how deep the graph
# already is ("fast" < "deep") and skip only when it's already at least as deep as this engine
# would make it -- so `--deep off` runs tree-sitter only, `--deep inline` runs Roslyn right after,
# yet a genuine re-ingest of an unchanged, already-deep project stays a no-op.
_DEPTH_RANK = {None: 0, "": 0, "fast": 1, "deep": 2}

_PARSE_CACHE_VERSION = 2
"""Bump when what `parse_source` extracts into a `RawFile` changes shape, so a stale
on-disk pickle cache (which bypasses dataclass defaults on unpickle) can't feed back
`RawFile`s missing a newly added field. v2 = the `doc` property was added. The version is
part of the cache dir name, so bumping it simply starts a fresh cache."""

# DEFAULT_CACHE_ROOT and repos_cache_dir now live in core/git_source.py (shared infra,
# so nothing in core has to import an ingestor to find them); re-exported here so the
# viewer and CLI that already import them from this module keep working unchanged.
__all__ = ["DEFAULT_CACHE_ROOT", "repos_cache_dir", "GitCSharpProjectConfig", "SyncStats", "sync", "sync_at"]

# _EXCLUDED_DIR_NAMES / the C# suffixes ((.cs, .cshtml)) are defined once in plugin.py (the
# import-light registration module) and imported above -- a single source of truth shared by
# the lazy GitLanguage registration and the .cs walk below.


@dataclass(frozen=True)
class GitCSharpProjectConfig:
    repo_uri: str
    project_name: str
    solution_name: str | None = None
    target_framework: str | None = None
    dto_name_suffixes: tuple[str, ...] = ("Dto", "Request", "Response", "ViewModel")
    workers: int | None = None


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
    """.cs files actually parsed this sync -- i.e. blob shas the parse cache had
    never seen before, regardless of path/commit/branch."""
    files_cached: int = 0
    """.cs files reused verbatim from the parse cache (identical content already
    parsed under some other path/commit/branch)."""


def sync(
    writer: GraphWriter,
    config: GitCSharpProjectConfig,
    ref: str | None,
    cache_root: Path | None = None,
    engine: str = DEFAULT_ENGINE,
) -> SyncStats:
    """Ingest (first run) or move (every run after) a git-tracked C# project's graph
    to `ref`. Fetches the repo, resolves `ref` to a commit sha, then delegates the
    per-commit work to sync_at()."""
    repo = GitRepoCache(config.repo_uri, repos_cache_dir(cache_root))
    repo.fetch()
    target_sha = repo.resolve_ref(ref)
    return sync_at(writer, config, repo, target_sha, ref, cache_root, engine)


def sync_at(
    writer: GraphWriter,
    config: GitCSharpProjectConfig,
    repo: GitRepoCache,
    target_sha: str,
    ref: str | None,
    cache_root: Path | None = None,
    engine: str = DEFAULT_ENGINE,
) -> SyncStats:
    """The per-commit half of sync(), given an already-fetched `repo` and an
    already-resolved `target_sha`. Split out so the multi-language ingestion
    endpoint (core/ingest.py) can fetch and resolve a repo once, then drive
    several languages against that same commit without each one re-fetching.

    `engine` selects the C# extractor: "roslyn" (semantic + DI resolution, the
    default) or "treesitter" (syntax-only fallback). "roslyn" falls back to
    tree-sitter automatically when the .NET SDK / extractor tool is unavailable."""
    started = time.monotonic()
    proj_id = project_id(config.project_name, config.solution_name)

    # setup_schema() must run before any read below -- FalkorDB's RO_QUERY raises
    # ("Invalid graph operation on empty key") against a graph nothing has ever
    # been written to; creating an index is what brings the graph key into
    # existence, same ordering BaseIngestor.run() already relies on.
    writer.setup_schema(NODE_SCHEMAS)

    current_sha = writer.get_property("Project", {"id": proj_id}, "commit_sha")
    current_depth = writer.get_property("Project", {"id": proj_id}, "depth")
    requested_depth = "deep" if engine == "roslyn" else "fast"
    if current_sha == target_sha and _DEPTH_RANK.get(current_depth, 0) >= _DEPTH_RANK[requested_depth]:
        return SyncStats(commit_sha=target_sha, elapsed_seconds=time.monotonic() - started)

    if engine == "roslyn":
        try:
            # A genuine same-commit fast->deep UPGRADE (tree-sitter just ran at this commit) must PRESERVE
            # the fast pass's extern:// call placeholders: Roslyn can occasionally fail to bind a call the
            # syntactic fast pass represented as a placeholder, and deleting it would stop a later producer
            # ingest from healing the cross-repo reference (PR #17 review). A standalone Roslyn resync is
            # authoritative instead, so it still diffs stale extern edges away.
            is_upgrade = current_sha == target_sha and current_depth == "fast"
            return _roslyn_sync_at(writer, config, repo, target_sha, ref, proj_id, started,
                                   diff_extern_edges=not is_upgrade)
        except roslyn.RoslynUnavailable as e:
            logger.warning("Roslyn engine unavailable (%s); falling back to tree-sitter", e)
    return _treesitter_sync_at(writer, config, repo, target_sha, ref, cache_root, proj_id, started)


def _project_items(
    config: GitCSharpProjectConfig, proj_id: str, target_sha: str, ref: str | None, depth: str
) -> list[GraphNode | GraphEdge]:
    """The Project (and optional Solution) root nodes -- identical across engines, so
    the viewer's git-projects panel, the depth-aware short-circuit, and re-sync all
    behave the same however the project was extracted. `depth` is "fast" (tree-sitter) or
    "deep" (Roslyn); it records how deep this graph is for the two-phase short-circuit."""
    items: list[GraphNode | GraphEdge] = [
        GraphNode(
            "Project",
            {"id": proj_id},
            {
                "name": config.project_name,
                "solution_name": config.solution_name,
                "target_framework": config.target_framework,
                "repo_uri": config.repo_uri,
                "commit_sha": target_sha,
                "depth": depth,  # "fast" (tree-sitter) or "deep" (Roslyn) -- see sync_at short-circuit
                "ref": ref,
                "dto_name_suffixes": list(config.dto_name_suffixes),
            },
        )
    ]
    if config.solution_name:
        solution_id = f"csharp://{config.solution_name}"
        items.append(GraphNode("Solution", {"id": solution_id}, {"name": config.solution_name}))
        items.append(GraphEdge("HAS_PROJECT", "Solution", {"id": solution_id}, "Project", {"id": proj_id}))
    return items


def _prune_stale_endpoint_edges(writer: GraphWriter, proj_id: str, target_items: list[GraphNode | GraphEdge]) -> None:
    """Delete THIS project's declared seam edges to a route-keyed (`extern://http/`) endpoint that the
    freshly-computed target no longer emits.

    `stream_apply`'s delta only tracks edges whose BOTH endpoints share the project id-prefix (see
    read_fingerprints), but a declared endpoint is deliberately route-keyed OUTSIDE any project's
    prefix so callers in every repo converge on one node (ADR 0006 dedup). That puts its `EXPOSES`
    (Method -> endpoint) and `ACCEPTS`/`RETURNS` (endpoint -> DTO) edges out of the delta's sight: an
    endpoint REMOVED with its method is cleaned by the method's DETACH, but an in-place route/verb
    CHANGE (method persists, its route attribute changes -> new endpoint id) would leave the old edge
    behind, the old endpoint would never orphan, and a duplicate would accumulate. This prunes those
    stale edges; the now-orphaned endpoint node is reclaimed by the http-endpoints stitch's GC.

    Idempotent: on an unchanged resync the keep-set equals what the graph holds, so nothing is deleted.
    Only this project's edges are touched (Method / DTO scoped to `proj_id`), and never the stitch's own
    `http-stitch` EXPOSES (which it rebuilds every run)."""
    keep_exposes = [[e.from_key["id"], e.to_key["id"]]
                    for e in target_items if isinstance(e, GraphEdge)
                    and e.rel_type == "EXPOSES_ENDPOINT" and str(e.to_key["id"]).startswith("extern://http/")]
    keep_dto = [[e.rel_type, e.from_key["id"], e.to_key["id"]]
                for e in target_items if isinstance(e, GraphEdge)
                and e.rel_type in ("ACCEPTS", "RETURNS") and str(e.from_key["id"]).startswith("extern://http/")]
    writer.query(
        "MATCH (m)-[r:EXPOSES_ENDPOINT]->(e:APIEndpoint) "
        "WHERE m.id STARTS WITH $p AND e.id STARTS WITH 'extern://http/' "
        "AND coalesce(r.provenance, '') <> 'http-stitch' "
        "AND NOT [m.id, e.id] IN $keep DELETE r",
        {"p": proj_id, "keep": keep_exposes},
    )
    writer.query(
        "MATCH (e:APIEndpoint)-[r:ACCEPTS|RETURNS]->(d) "
        "WHERE e.id STARTS WITH 'extern://http/' AND d.id STARTS WITH $p "
        "AND NOT [type(r), e.id, d.id] IN $keep DELETE r",
        {"p": proj_id, "keep": keep_dto},
    )


def _apply(
    writer: GraphWriter, proj_id: str, target_items: list[GraphNode | GraphEdge], target_sha: str, started: float,
    files_parsed: int, files_cached: int, *, diff_extern_edges: bool = True
) -> SyncStats:
    # Cross-repo reconciliation (ADR 0004): fold placeholder<->definer pairs among the linkages
    # this ingest touched -- targeted index seeks, no scan. No-op unless the graph holds >1 repo.
    from auryn.core.linkage import reconcile_linkages

    # kendo.extract_ui inspects the target to derive UI edges, so csharp (unlike the streaming
    # resolvers) builds target_items up front. stream_apply still avoids the second full copy
    # compute_delta would make; it iterates the list twice (nodes pass, then edges pass), so a
    # fresh iterator each call is required -- hence `lambda: iter(target_items)`. `diff_extern_edges`
    # is False only for a same-commit fast->deep Roslyn upgrade (preserve the fast pass's extern://
    # placeholders Roslyn may not re-author); the fast/standalone pass is authoritative (True).
    labels = [schema.label for schema in NODE_SCHEMAS]
    stats, reals, placeholders = stream_apply(writer, proj_id, labels, lambda: iter(target_items),
                                              diff_extern_edges=diff_extern_edges)
    reconcile_linkages(writer, reals, placeholders)
    _prune_stale_endpoint_edges(writer, proj_id, target_items)

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


def _treesitter_sync_at(
    writer: GraphWriter,
    config: GitCSharpProjectConfig,
    repo: GitRepoCache,
    target_sha: str,
    ref: str | None,
    cache_root: Path | None,
    proj_id: str,
    started: float,
) -> SyncStats:
    workers = config.workers or os.cpu_count() or 1
    parse_cache_dir = (cache_root or cache_home()) / f"parse_cache_csharp.v{_PARSE_CACHE_VERSION}"
    raw_files, files_parsed, files_cached = _build_raw_files(repo, target_sha, parse_cache_dir, workers)

    target_items = _project_items(config, proj_id, target_sha, ref, "fast")
    target_items.extend(resolution.resolve(raw_files, proj_id, config.dto_name_suffixes))
    target_items.extend(kendo.extract_ui(repo, target_sha, proj_id, target_items))
    return _apply(writer, proj_id, target_items, target_sha, started, files_parsed, files_cached)


def _roslyn_sync_at(
    writer: GraphWriter,
    config: GitCSharpProjectConfig,
    repo: GitRepoCache,
    target_sha: str,
    ref: str | None,
    proj_id: str,
    started: float,
    *,
    diff_extern_edges: bool = True,
) -> SyncStats:
    """Get a real directory for the tree at `target_sha` and run the Roslyn extractor
    over it. The tool needs real files on disk (it builds a Compilation), unlike
    tree-sitter's blob streaming, so there's no per-file blob cache here -- the whole
    solution is re-analyzed each sync (see the plan's scale note). `working_tree`
    materializes a temp checkout for a git repo and yields the directory itself for a
    local source, so this path is identical either way."""
    with repo.working_tree(target_sha) as workdir:
        items, metrics = roslyn.extract(
            str(workdir),
            config.project_name,
            solution_name=config.solution_name,
            commit_sha=target_sha,
            target_framework=config.target_framework,
            dto_suffixes=config.dto_name_suffixes,
        )

    # Swap the tool's root nodes for our canonical ones (full props: repo_uri, ref,
    # dto_name_suffixes) so the Project node matches the tree-sitter engine's exactly.
    body = [item for item in items if not _is_root_item(item)]
    target_items = _project_items(config, proj_id, target_sha, ref, "deep") + body
    target_items.extend(kendo.extract_ui(repo, target_sha, proj_id, target_items))
    return _apply(writer, proj_id, target_items, target_sha, started, metrics.get("filesParsed", 0), 0,
                  diff_extern_edges=diff_extern_edges)


def _is_root_item(item: GraphNode | GraphEdge) -> bool:
    if isinstance(item, GraphNode):
        return item.label in ("Project", "Solution")
    return item.rel_type == "HAS_PROJECT"


def _parse_blob(entry: tuple[str, bytes]) -> RawFile:
    path, source = entry
    return parse_source(source, path)


def _cache_path(parse_cache_dir: Path, blob_sha: str) -> Path:
    return parse_cache_dir / f"{blob_sha}.pickle"


def _load_cached(parse_cache_dir: Path, blob_sha: str) -> RawFile | None:
    path = _cache_path(parse_cache_dir, blob_sha)
    if not path.exists():
        return None
    with path.open("rb") as f:
        return pickle.load(f)


def _store_cached(parse_cache_dir: Path, blob_sha: str, raw_file: RawFile) -> None:
    parse_cache_dir.mkdir(parents=True, exist_ok=True)
    with _cache_path(parse_cache_dir, blob_sha).open("wb") as f:
        pickle.dump(raw_file, f)


def _build_raw_files(
    repo: GitRepoCache, sha: str, parse_cache_dir: Path, workers: int
) -> tuple[list[RawFile], int, int]:
    """Every .cs file at `sha`, parsed. Content identical to something already
    parsed under any other path/commit/branch is never re-parsed -- the cache is
    keyed by blob sha, not path, so a pure rename is a cache hit too (just
    re-stamped with the current tree's path for that blob). Returns
    (raw_files, files_parsed, files_cached) -- the last two are pure reporting,
    surfaced on SyncStats so a sync's actual cache effectiveness is visible rather
    than just inferred from wall time."""
    entries = repo.list_files(sha, ".cs", _EXCLUDED_DIR_NAMES)

    raw_files: list[RawFile] = []
    to_parse: list[tuple[str, str]] = []
    for path, blob_sha in entries:
        cached = _load_cached(parse_cache_dir, blob_sha)
        if cached is not None:
            cached.path = path
            raw_files.append(cached)
        else:
            to_parse.append((path, blob_sha))

    files_cached = len(raw_files)
    if not to_parse:
        return raw_files, 0, files_cached

    blobs = [(path, repo.read_blob(blob_sha)) for path, blob_sha in to_parse]
    if progress.verbose_enabled():
        # Verbose: parse sequentially, logging each file *before* parsing it, so a native crash
        # (BrokenProcessPool) leaves the offending file as the last logged line -- a worker pool
        # can't pinpoint it (several files in flight at once). See the PL/SQL equivalent.
        logger.info("Verbose: parsing %d file(s) sequentially so a crash pinpoints the file", len(blobs))
        parsed = []
        for i, b in enumerate(blobs, 1):
            progress.log_file(logger, b[0], i, len(blobs))
            parsed.append(_parse_blob(b))
    elif workers == 1 or len(blobs) <= 1:
        parsed = [_parse_blob(b) for b in blobs]
    else:
        # Preload the parse module (parse_source's home) into the forkserver control process so
        # workers inherit its tree-sitter import via COW rather than re-importing -- see core.parallel.
        with make_process_pool(workers, preload=["auryn.ingestors.csharp.file_parser"]) as pool:
            parsed = list(pool.map(_parse_blob, blobs))

    for (_, blob_sha), raw_file in zip(to_parse, parsed):
        _store_cached(parse_cache_dir, blob_sha, raw_file)
        raw_files.append(raw_file)

    return raw_files, len(to_parse), files_cached


def _git_language_sync(
    writer: GraphWriter,
    repo: GitRepoCache,
    target_sha: str,
    *,
    engine: str = DEFAULT_ENGINE,
    repo_uri: str,
    project_name: str,
    solution_name: str | None,
    ref: str | None,
    cache_root: Path | None = None,
) -> LanguageIngestResult:
    """GitLanguage adapter for the multi-language ingestion endpoint: builds a
    GitCSharpProjectConfig from the endpoint's language-agnostic inputs (the
    C#-specific options -- dto suffixes, target framework, workers -- take their
    defaults), runs sync_at() against the already-fetched repo/commit with `engine`, and
    maps SyncStats onto the uniform LanguageIngestResult. `engine` picks the phase:
    "treesitter" (fast) or "roslyn" (deep) -- both target the same commit and Project id, so
    running roslyn after treesitter is an in-place upgrade via recompute-and-diff."""
    config = GitCSharpProjectConfig(
        repo_uri=repo_uri, project_name=project_name, solution_name=solution_name
    )
    s = sync_at(writer, config, repo, target_sha, ref, cache_root, engine=engine)
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


def treesitter_language_sync(writer, repo, target_sha, **kwargs) -> LanguageIngestResult:
    """Fast phase (tree-sitter, no .NET SDK) -- the GitLanguage's `sync`. See plugin.py."""
    return _git_language_sync(writer, repo, target_sha, engine="treesitter", **kwargs)


def roslyn_language_sync(writer, repo, target_sha, **kwargs) -> LanguageIngestResult:
    """Deep phase (Roslyn: semantic analysis + DI resolution) -- the GitLanguage's `deep_sync`.
    Layers over the fast pass in place (same Project id / recompute-and-diff); falls back to
    tree-sitter automatically when the .NET SDK / extractor tool is unavailable."""
    return _git_language_sync(writer, repo, target_sha, engine="roslyn", **kwargs)


# The GitLanguage is registered by the import-light plugin.py (the entry-point target),
# whose lazy `sync`/`deep_sync` thunks call these -- so importing this heavy engine module
# (tree-sitter, the Roslyn extractor) is deferred until a source has C# files.
