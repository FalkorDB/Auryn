"""Git-tracked (and directory) Helm ingestion: fully recompute the project's Helm subgraph at
`target_sha` and diff it onto the graph (graph_sync.stream_apply). v1 is full-recompute and
render-free. Registered as the `helm` GitLanguage by the import-light plugin.py.

A chart is anchored by its `Chart.yaml`; each chart pulls its sibling `values.yaml` and NOTHING else
-- the Go-templated `templates/*.yaml` are not read (ADR 0007 line 225). The GitLanguage suffix match
is broad (`*.yaml`/`*.yml`, like k8s) -- the `Chart.yaml` basename is what actually identifies a chart
here, and non-chart YAML yields nothing, so a repo with no charts never gets a phantom `helm://`
project. (A tighter filename-aware detector is the `GitLanguage.filename_matches` follow-up, ADR §7.)

The k8s producer naturally skips these same files, so there is no double-ingest: `Chart.yaml`/
`values.yaml` lack `apiVersion`+`kind` (k8s's byte pre-check drops them), and a chart's templates fail
k8s's YAML parse (the `{{ }}` makes them invalid YAML). The two producers coexist on distinct
`helm://`/`k8s://` id prefixes."""

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
from auryn.ingestors.helm.plugin import CHART_GLOBS as _CHART_GLOBS
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


def _collect_charts(source, target_sha: str) -> dict[str, tuple]:
    """Group the repo's YAML into charts: `{chart_dir: (chart_blob, values_blob)}` keyed off each
    `Chart.yaml`. Only the chart's own `Chart.yaml` + sibling `values.yaml` are kept -- v1 reads no
    templates. A repo YAML file that isn't a `Chart.yaml` (and isn't a chart's `values.yaml`) is
    dropped, so a plain k8s manifest or a compose file never anchors a helm chart."""
    yaml_files: dict[str, str] = {}  # only Chart.yaml + values.yaml, not every repo YAML
    for path, blob_sha in source.list_files_glob(target_sha, _CHART_GLOBS, set(_EXCLUDED_DIR_NAMES)):
        yaml_files.setdefault(path, blob_sha)

    chart_dirs: dict[str, str] = {}
    for p in yaml_files:
        if posixpath.basename(p) != "Chart.yaml":
            continue
        cd = posixpath.dirname(p)
        norm = cd.replace(chr(92), "/").strip("/")  # a non-root chart literally at `_root`/`_dep/...`
        if norm == "_root" or norm == "_dep" or norm.startswith("_dep/"):
            logger.warning("helm: chart dir %r collides with a reserved id namespace (_root/_dep) -- skipped", cd)
            continue
        chart_dirs[cd] = yaml_files[p]
    charts: dict[str, tuple] = {}
    for cd, chart_blob in sorted(chart_dirs.items()):
        values_blob = yaml_files.get(posixpath.join(cd, "values.yaml") if cd else "values.yaml")
        charts[cd] = (chart_blob, values_blob)
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
    for cd, (chart_blob, values_blob) in _collect_charts(source, target_sha).items():
        try:
            parsed_charts.append(parse_chart(cd, _read(chart_blob), _read(values_blob)))
        except Exception:
            # KNOWN LIMITATION (tier-wide): a failed read drops the chart from the target, so the
            # recompute REMOVES its prior nodes -- no keep-last-good like the tree-sitter ingestors.
            logger.warning("failed to read chart at %s (its nodes are removed this sync)", cd or "<root>", exc_info=True)
            continue

    # Don't stamp a phantom `helm://<project>` on a repo that has no charts at all. Still proceed when a
    # prior helm subgraph exists, so removing the last chart cleans it (recompute-to-empty).
    if not parsed_charts and prior_sha is None:
        return SyncStats(commit_sha=target_sha, elapsed_seconds=time.monotonic() - started)

    project_node = GraphNode(
        "Project", {"id": proj_id},
        {"name": project_name, "repo_uri": repo_uri, "commit_sha": target_sha, "ref": ref},
    )

    def make_target():
        # Project node LAST, so commit_sha isn't stamped on a partial graph (see k8s git_sync).
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
