"""Git-tracked (and directory) Helm ingestion: fully recompute the project's Helm subgraph at
`target_sha` and diff it onto the graph (graph_sync.stream_apply). v1 is full-recompute and
render-free. Registered as the `helm` GitLanguage by the import-light plugin.py.

A chart is anchored by its `Chart.yaml`; each chart pulls its sibling `values.yaml` and its
`templates/*.yaml`. The GitLanguage suffix match is broad (`*.yaml`/`*.yml`, like k8s) -- the
`Chart.yaml` basename is what actually identifies a chart here, and non-chart YAML yields nothing. (A
tighter filename-aware detector is the `GitLanguage.filename_matches` follow-up in ADR 0007 §7.)"""

from __future__ import annotations

import itertools
import logging
import posixpath
import time
from dataclasses import dataclass

from auryn.core.graph_model import GraphNode
from auryn.core.graph_sync import stream_apply
from auryn.core.registry import LanguageIngestResult
from auryn.ingestors.helm.build import build_graph
from auryn.ingestors.helm.file_parser import parse_chart
from auryn.ingestors.helm.ids import project_id
from auryn.ingestors.helm.plugin import EXCLUDED_DIR_NAMES as _EXCLUDED_DIR_NAMES
from auryn.ingestors.helm.plugin import FILE_SUFFIXES as _FILE_SUFFIXES
from auryn.ingestors.helm.schema import NODE_SCHEMAS, OWNED_LABELS

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


def _templates_prefix(chart_dir: str) -> str:
    return f"{chart_dir}/templates/" if chart_dir else "templates/"


def _collect_charts(source, target_sha: str):
    """Group the repo's YAML into charts: `{chart_dir: (chart_blob, values_blob, [(tpl_path, blob)])}`
    keyed off each `Chart.yaml`. Only files under a chart's own `templates/` are its templates (a nested
    subchart keeps its own)."""
    yaml_files: dict[str, str] = {}
    for suffix in _FILE_SUFFIXES:
        for path, blob_sha in source.list_files(target_sha, suffix, set(_EXCLUDED_DIR_NAMES)):
            yaml_files.setdefault(path, blob_sha)

    chart_dirs = {posixpath.dirname(p): yaml_files[p] for p in yaml_files if posixpath.basename(p) == "Chart.yaml"}
    charts: dict[str, tuple] = {}
    for cd, chart_blob in sorted(chart_dirs.items()):
        values_blob = yaml_files.get(posixpath.join(cd, "values.yaml") if cd else "values.yaml")
        tprefix = _templates_prefix(cd)
        templates = sorted((p, sha) for p, sha in yaml_files.items() if p.startswith(tprefix))
        charts[cd] = (chart_blob, values_blob, templates)
    return charts


def sync_at(writer, source, target_sha: str, *, project_name: str, repo_uri: str, ref: str | None) -> SyncStats:
    started = time.monotonic()
    proj_id = project_id(project_name)

    writer.setup_schema(NODE_SCHEMAS)
    prior_sha = writer.get_property("Project", {"id": proj_id}, "commit_sha")
    if prior_sha == target_sha:  # tier-wide limitation: ignores source file-selection (see k8s git_sync)
        return SyncStats(commit_sha=target_sha, elapsed_seconds=time.monotonic() - started)

    def _read(blob_sha):
        return source.read_blob(blob_sha) if blob_sha else None

    parsed_charts = []
    for cd, (chart_blob, values_blob, templates) in _collect_charts(source, target_sha).items():
        try:
            tpls = [(p, source.read_blob(sha)) for p, sha in templates]
            parsed_charts.append(parse_chart(cd, _read(chart_blob), _read(values_blob), tpls))
        except Exception:
            # KNOWN LIMITATION (tier-wide): a failed read drops the chart from the target, so the
            # recompute REMOVES its prior nodes -- no keep-last-good.
            logger.warning("failed to read chart at %s (its nodes are removed this sync)", cd or "<root>", exc_info=True)
            continue

    if not parsed_charts and prior_sha is None:
        return SyncStats(commit_sha=target_sha, elapsed_seconds=time.monotonic() - started)

    project_node = GraphNode(
        "Project", {"id": proj_id},
        {"name": project_name, "repo_uri": repo_uri, "commit_sha": target_sha, "ref": ref},
    )

    def make_target():
        return itertools.chain(build_graph(parsed_charts, proj_id), [project_node])

    stats, _r, _p = stream_apply(writer, proj_id, OWNED_LABELS, make_target)
    elapsed = time.monotonic() - started
    logger.info("helm: %d chart(s), +%dn/%de in %.2fs", len(parsed_charts), stats.nodes_added, stats.edges_added, elapsed)
    return SyncStats(
        nodes_added=stats.nodes_added, nodes_updated=stats.nodes_updated, nodes_removed=stats.nodes_removed,
        edges_added=stats.edges_added, edges_updated=stats.edges_updated, edges_removed=stats.edges_removed,
        commit_sha=target_sha, elapsed_seconds=elapsed, files_parsed=len(parsed_charts),
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
