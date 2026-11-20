"""Git-tracked (and directory) CI ingestion: fully recompute the project's CI subgraph at `target_sha`
and diff it onto the graph (graph_sync.stream_apply). v1 is full-recompute and static. Registered as
the `ci` GitLanguage by the import-light plugin.py.

Only `.github/workflows/*.yml`/`.yaml` are read (the path-anchored `file_globs`); a file there without
a `jobs:` map parses to an invalid workflow and is dropped. Removing the last workflow is handled by
the `ci://` prior-graph cleanup in core.ingest."""

from __future__ import annotations

import itertools
import logging
import time
from dataclasses import dataclass

from auryn.core.graph_model import GraphNode
from auryn.core.graph_sync import stream_apply
from auryn.core.registry import LanguageIngestResult
from auryn.ingestors.ci.build import build_graph
from auryn.ingestors.ci.file_parser import parse_workflow
from auryn.ingestors.ci.ids import project_id
from auryn.ingestors.ci.plugin import EXCLUDED_DIR_NAMES as _EXCLUDED_DIR_NAMES
from auryn.ingestors.ci.plugin import FILE_GLOBS as _FILE_GLOBS
from auryn.ingestors.ci.schema import NODE_SCHEMAS, OWNED_LABELS

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
    prior_sha = writer.get_property("Project", {"id": proj_id}, "commit_sha")
    if prior_sha == target_sha:  # tier-wide limitation: ignores source file-selection (see k8s git_sync)
        return SyncStats(commit_sha=target_sha, elapsed_seconds=time.monotonic() - started)

    parsed = []
    for path, blob_sha in sorted(source.list_files_glob(target_sha, _FILE_GLOBS, set(_EXCLUDED_DIR_NAMES))):
        try:
            pw = parse_workflow(source.read_blob(blob_sha))
        except Exception:
            # KNOWN LIMITATION (tier-wide): a failed read drops it, so the recompute removes its nodes.
            logger.warning("failed to read workflow %s (its nodes are removed this sync)", path, exc_info=True)
            continue
        if pw.valid:
            parsed.append((path, pw))

    # Gate the empty-sync phantom guard on the Project NODE existing, not on `commit_sha` (an interrupted
    # resync can clear `commit_sha` while leaving nodes, which must still be cleaned up).
    if not parsed and not writer.node_exists("Project", proj_id):
        return SyncStats(commit_sha=target_sha, elapsed_seconds=time.monotonic() - started)

    project_node = GraphNode(
        "Project", {"id": proj_id},
        {"name": project_name, "repo_uri": repo_uri, "commit_sha": target_sha, "ref": ref},
    )

    def make_target():
        # Project node LAST (see k8s git_sync); fully empty target when the last workflow is gone, so the
        # stale `ci://` subgraph is removed in its entirety (no bare Project node left behind).
        tail = [project_node] if parsed else []
        return itertools.chain(build_graph(parsed, proj_id), tail)

    stats, _r, _p = stream_apply(writer, proj_id, OWNED_LABELS, make_target)
    elapsed = time.monotonic() - started
    logger.info("ci: %d workflow(s), +%dn/%de in %.2fs", len(parsed), stats.nodes_added, stats.edges_added, elapsed)
    return SyncStats(
        nodes_added=stats.nodes_added, nodes_updated=stats.nodes_updated, nodes_removed=stats.nodes_removed,
        edges_added=stats.edges_added, edges_updated=stats.edges_updated, edges_removed=stats.edges_removed,
        commit_sha=target_sha, elapsed_seconds=elapsed, files_parsed=len(parsed),
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
