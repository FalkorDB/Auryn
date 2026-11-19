"""Git-tracked (and directory) Dockerfile ingestion: fully recompute the project's Docker subgraph
at `target_sha` and diff it onto the graph (graph_sync.stream_apply) -- the same recompute-and-delta
model every file-based ingestor uses. v1 is full-recompute only (Dockerfiles are few and tiny, so
parsing is cheap and parallelism/incremental routing is unnecessary); an incremental path like the
shared `route_incremental` engine would slot in here exactly as the language ingestors do.

Registered as the `docker` GitLanguage by the import-light plugin.py, so this module (the parser) is
imported only when a source actually contains Dockerfiles."""

from __future__ import annotations

import itertools
import logging
import time
from dataclasses import dataclass

from auryn.core.graph_model import GraphNode
from auryn.core.graph_sync import stream_apply
from auryn.core.registry import LanguageIngestResult
from auryn.ingestors.docker.build import build_graph
from auryn.ingestors.docker.file_parser import parse_source
from auryn.ingestors.docker.ids import project_id
from auryn.ingestors.docker.plugin import EXCLUDED_DIR_NAMES as _EXCLUDED_DIR_NAMES
from auryn.ingestors.docker.plugin import MATCH_PATTERNS as _MATCH_PATTERNS
from auryn.ingestors.docker.schema import NODE_SCHEMAS, OWNED_LABELS

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


def sync_at(writer, source, target_sha: str, *, project_name: str, repo_uri: str, ref: str | None) -> SyncStats:
    """Recompute the whole Docker subgraph at `target_sha` and delta it. `source` is duck-typed
    (GitRepoCache or LocalDirectorySource): only list_files/read_blob are used."""
    started = time.monotonic()
    proj_id = project_id(project_name)

    writer.setup_schema(NODE_SCHEMAS)  # brings the graph key + indexes into existence
    if writer.get_property("Project", {"id": proj_id}, "commit_sha") == target_sha:
        return SyncStats(commit_sha=target_sha, elapsed_seconds=time.monotonic() - started)

    seen: dict[str, str] = {}  # path -> blob_sha (dedup across patterns)
    for path, blob_sha in source.list_files_glob(target_sha, _MATCH_PATTERNS, set(_EXCLUDED_DIR_NAMES)):
        seen.setdefault(path, blob_sha)

    files = []
    for path, blob_sha in sorted(seen.items()):
        try:
            files.append((path, parse_source(source.read_blob(blob_sha))))
        except Exception:
            # A single malformed Dockerfile never fails the whole ingest. KNOWN LIMITATION (tracked
            # follow-up): a parse/read failure drops the file from the recomputed target, so this
            # sync REMOVES its prior nodes -- it does not keep last-good like the tree-sitter
            # ingestors' `broken_files` path. Adopting that keep-last-good behavior here is deferred.
            logger.warning("failed to parse Dockerfile %s (its graph nodes are removed this sync)", path, exc_info=True)

    project_node = GraphNode(
        "Project", {"id": proj_id},
        {"name": project_name, "repo_uri": repo_uri, "commit_sha": target_sha, "ref": ref},
    )

    def make_target():  # re-iterable: stream_apply runs it once for nodes, once for edges
        return itertools.chain([project_node], build_graph(files, proj_id))

    stats, _reals, _placeholders = stream_apply(writer, proj_id, OWNED_LABELS, make_target)
    elapsed = time.monotonic() - started
    logger.info("docker: %d Dockerfile(s), +%dn/%de in %.2fs",
                len(files), stats.nodes_added, stats.edges_added, elapsed)
    return SyncStats(
        nodes_added=stats.nodes_added, nodes_updated=stats.nodes_updated, nodes_removed=stats.nodes_removed,
        edges_added=stats.edges_added, edges_updated=stats.edges_updated, edges_removed=stats.edges_removed,
        commit_sha=target_sha, elapsed_seconds=elapsed, files_parsed=len(files),
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
) -> LanguageIngestResult:
    """GitLanguage adapter for the multi-language endpoint (Docker has no solution/cache knobs --
    accepted for the shared interface and ignored)."""
    s = sync_at(writer, source, target_sha, project_name=project_name, repo_uri=repo_uri, ref=ref)
    return LanguageIngestResult(
        nodes_added=s.nodes_added, nodes_updated=s.nodes_updated, nodes_removed=s.nodes_removed,
        edges_added=s.edges_added, edges_updated=s.edges_updated, edges_removed=s.edges_removed,
        files_parsed=s.files_parsed, files_cached=s.files_cached,
        commit_sha=s.commit_sha, elapsed_seconds=s.elapsed_seconds,
    )
