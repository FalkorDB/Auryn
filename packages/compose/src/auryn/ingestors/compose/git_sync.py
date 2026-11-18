"""Git-tracked (and directory) docker-compose ingestion: fully recompute the project's Compose
subgraph at `target_sha` and diff it onto the graph (graph_sync.stream_apply) -- the same
recompute-and-delta model every file-based ingestor uses. v1 is full-recompute only (compose files
are few and tiny). Registered as the `compose` GitLanguage by the import-light plugin.py."""

from __future__ import annotations

import itertools
import logging
import time
from dataclasses import dataclass

from auryn.core.graph_model import GraphNode
from auryn.core.graph_sync import stream_apply
from auryn.core.registry import LanguageIngestResult
from auryn.ingestors.compose.build import build_graph
from auryn.ingestors.compose.file_parser import parse_source
from auryn.ingestors.compose.ids import project_id
from auryn.ingestors.compose.plugin import EXCLUDED_DIR_NAMES as _EXCLUDED_DIR_NAMES
from auryn.ingestors.compose.plugin import FILE_SUFFIXES as _FILE_SUFFIXES
from auryn.ingestors.compose.schema import NODE_SCHEMAS, OWNED_LABELS

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
    started = time.monotonic()
    proj_id = project_id(project_name)

    writer.setup_schema(NODE_SCHEMAS)
    if writer.get_property("Project", {"id": proj_id}, "commit_sha") == target_sha:
        return SyncStats(commit_sha=target_sha, elapsed_seconds=time.monotonic() - started)

    seen: dict[str, str] = {}
    for suffix in _FILE_SUFFIXES:
        for path, blob_sha in source.list_files(target_sha, suffix, set(_EXCLUDED_DIR_NAMES)):
            seen.setdefault(path, blob_sha)

    files = []
    for path, blob_sha in sorted(seen.items()):
        try:
            files.append((path, parse_source(source.read_blob(blob_sha))))
        except Exception:
            logger.warning("failed to parse compose file %s (its nodes are removed this sync)", path, exc_info=True)

    project_node = GraphNode(
        "Project", {"id": proj_id},
        {"name": project_name, "repo_uri": repo_uri, "commit_sha": target_sha, "ref": ref},
    )

    def make_target():
        return itertools.chain([project_node], build_graph(files, proj_id))

    stats, _r, _p = stream_apply(writer, proj_id, OWNED_LABELS, make_target)
    elapsed = time.monotonic() - started
    logger.info("compose: %d file(s), +%dn/%de in %.2fs", len(files), stats.nodes_added, stats.edges_added, elapsed)
    return SyncStats(
        nodes_added=stats.nodes_added, nodes_updated=stats.nodes_updated, nodes_removed=stats.nodes_removed,
        edges_added=stats.edges_added, edges_updated=stats.edges_updated, edges_removed=stats.edges_removed,
        commit_sha=target_sha, elapsed_seconds=elapsed, files_parsed=len(files),
    )


def _git_language_sync(
    writer, source, target_sha: str, *, repo_uri: str, project_name: str,
    solution_name: str | None, ref: str | None, cache_root=None,
) -> LanguageIngestResult:
    s = sync_at(writer, source, target_sha, project_name=project_name, repo_uri=repo_uri, ref=ref)
    return LanguageIngestResult(
        nodes_added=s.nodes_added, nodes_updated=s.nodes_updated, nodes_removed=s.nodes_removed,
        edges_added=s.edges_added, edges_updated=s.edges_updated, edges_removed=s.edges_removed,
        files_parsed=s.files_parsed, files_cached=s.files_cached,
        commit_sha=s.commit_sha, elapsed_seconds=s.elapsed_seconds,
    )
