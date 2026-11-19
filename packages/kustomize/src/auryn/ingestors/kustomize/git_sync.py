"""Git-tracked (and directory) Kustomize ingestion: fully recompute the project's Kustomize subgraph
at `target_sha` and diff it onto the graph (graph_sync.stream_apply). v1 is full-recompute and
render-free. Registered as the `kustomize` GitLanguage by the import-light plugin.py.

Each kustomization is anchored by its `kustomization.yaml`/`.yml`/`Kustomization` basename; nothing
else is read (the base manifests it patches need a `kustomize build` render, a follow-up). Removing
the last kustomization is handled by the `kustomize://` prior-graph cleanup in core.ingest."""

from __future__ import annotations

import itertools
import logging
import posixpath
import time
from dataclasses import dataclass

from auryn.core.graph_model import GraphNode
from auryn.core.graph_sync import stream_apply
from auryn.core.registry import LanguageIngestResult
from auryn.ingestors.kustomize.build import build_graph
from auryn.ingestors.kustomize.file_parser import parse_kustomization
from auryn.ingestors.kustomize.ids import project_id
from auryn.ingestors.kustomize.plugin import EXCLUDED_DIR_NAMES as _EXCLUDED_DIR_NAMES
from auryn.ingestors.kustomize.plugin import FILE_GLOBS as _FILE_GLOBS
from auryn.ingestors.kustomize.schema import NODE_SCHEMAS, OWNED_LABELS

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
        cd = posixpath.dirname(path)
        if cd.replace(chr(92), "/").strip("/") == "_root":  # the reserved repo-root scope
            logger.warning("kustomize: dir %r collides with the reserved `_root` scope -- skipped", cd)
            continue
        try:
            blob = source.read_blob(blob_sha)
        except Exception:
            # KNOWN LIMITATION (tier-wide): a failed read drops it from the target, so the recompute
            # REMOVES its prior nodes -- no keep-last-good.
            logger.warning("failed to read kustomization %s (its nodes are removed this sync)", path, exc_info=True)
            continue
        parsed.append((path, parse_kustomization(posixpath.dirname(path), blob)))

    # Don't stamp a phantom `kustomize://<project>` on a repo with no kustomizations. Still proceed when
    # a prior subgraph exists, so removing the last one cleans it (recompute-to-empty). Gate on the
    # Project NODE existing, not on `commit_sha`: an interrupted resync can clear `commit_sha` while
    # leaving the nodes, and those must still be cleaned up (a bare `prior_sha is None` would skip them).
    if not parsed and not writer.node_exists("Project", proj_id):
        return SyncStats(commit_sha=target_sha, elapsed_seconds=time.monotonic() - started)

    project_node = GraphNode(
        "Project", {"id": proj_id},
        {"name": project_name, "repo_uri": repo_uri, "commit_sha": target_sha, "ref": ref},
    )

    def make_target():
        # Project node LAST, so commit_sha isn't stamped on a partial graph (see k8s git_sync). When the
        # last kustomization is gone, the target is fully empty -- no Project either -- so the stale
        # `kustomize://` subgraph is removed in its entirety.
        tail = [project_node] if parsed else []
        return itertools.chain(build_graph(parsed, proj_id), tail)

    stats, _r, _p = stream_apply(writer, proj_id, OWNED_LABELS, make_target)
    elapsed = time.monotonic() - started
    logger.info("kustomize: %d kustomization(s), +%dn/%de in %.2fs", len(parsed), stats.nodes_added, stats.edges_added, elapsed)
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
