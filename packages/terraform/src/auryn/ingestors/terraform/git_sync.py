"""Git-tracked (and directory) Terraform ingestion: fully recompute the project's Terraform subgraph
at `target_sha` and diff it onto the graph (graph_sync.stream_apply). v1 is full-recompute. Registered
as the `terraform` GitLanguage by the import-light plugin.py."""

from __future__ import annotations

import itertools
import logging
import time
from dataclasses import dataclass

from auryn.core.graph_model import GraphNode
from auryn.core.graph_sync import stream_apply
from auryn.core.registry import LanguageIngestResult
from auryn.ingestors.terraform.build import build_graph
from auryn.ingestors.terraform.file_parser import parse_source
from auryn.ingestors.terraform.ids import project_id
from auryn.ingestors.terraform.plugin import EXCLUDED_DIR_NAMES as _EXCLUDED_DIR_NAMES
from auryn.ingestors.terraform.plugin import MATCH_PATTERNS as _MATCH_PATTERNS
from auryn.ingestors.terraform.schema import NODE_SCHEMAS, OWNED_LABELS

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

    all_files = dict(source.list_files_glob(target_sha, _MATCH_PATTERNS, set(_EXCLUDED_DIR_NAMES)))
    # OpenTofu precedence: in a directory a `<name>.tofu` OVERRIDES the sibling `<name>.tf` (the `.tf` is
    # ignored), so drop a `.tf` whenever its `.tofu` twin is present -- otherwise both parse and the same
    # resource address yields a duplicate node.
    tofu_stems = {p.rsplit(".", 1)[0] for p in all_files if p.endswith(".tofu")}
    seen = {p: sha for p, sha in all_files.items()
            if not (p.endswith(".tf") and p.rsplit(".", 1)[0] in tofu_stems)}

    files = []
    for path, blob_sha in sorted(seen.items()):
        try:
            parsed = parse_source(source.read_blob(blob_sha))
        except Exception:
            logger.warning("failed to parse %s (its nodes are removed this sync)", path, exc_info=True)
            continue
        if parsed.blocks:
            files.append((path, parsed))

    # (a Project node without a stamp is a sync that died mid-apply, not "never ingested")
    if not files and prior_sha is None and not writer.node_exists("Project", proj_id):
        return SyncStats(commit_sha=target_sha, elapsed_seconds=time.monotonic() - started)

    project_node = GraphNode(
        "Project", {"id": proj_id},
        {"name": project_name, "repo_uri": repo_uri, "commit_sha": target_sha, "ref": ref},
    )

    def make_target():
        return itertools.chain(build_graph(files, proj_id), [project_node])

    stats, _r, _p = stream_apply(writer, proj_id, OWNED_LABELS, make_target)
    elapsed = time.monotonic() - started
    logger.info("terraform: %d .tf file(s), +%dn/%de in %.2fs", len(files), stats.nodes_added, stats.edges_added, elapsed)
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
