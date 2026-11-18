"""Turn parsed Helm charts into the deployment-tier graph (ADR 0007): the chart's `Chart.yaml` File
owns a `Resource` (the release); the chart DEPENDS_ON the charts it depends on (resolved to an in-repo
chart by name, else a placeholder), DEPLOYS the `Service` workloads its templates declare, and each
workload RUNS_IMAGE the shared `Image` hub (the image resolved from `values.yaml`).

Emits no cross-artifact edge beyond the shared `Image` hub; the `deploy-helm` enrichment runs the
image lifecycle. v1 is render-free -- no `helm template`."""

from __future__ import annotations

import posixpath
from collections.abc import Iterator

from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.core.image_ref import image_id, image_node
from auryn.ingestors.helm import ids
from auryn.ingestors.helm.model import ParsedChart


def _scope(pc: ParsedChart) -> str:
    return ids.chart_scope(pc.chart_dir, pc.chart.name if pc.chart else "")


def _chart_file(chart_dir: str) -> str:
    return posixpath.join(chart_dir, "Chart.yaml") if chart_dir else "Chart.yaml"


def build_graph(charts: list[ParsedChart], proj_id: str) -> Iterator[GraphNode | GraphEdge]:
    # Index chart NAME -> its Resource id, so a `dependencies:` entry naming an in-repo chart resolves
    # to a real DEPENDS_ON (an external dependency falls back to a project-scoped placeholder).
    by_name: dict[str, str] = {}
    for pc in charts:
        if pc.chart:
            by_name[pc.chart.name] = ids.chart_id(proj_id, _scope(pc))

    for pc in charts:
        scope = _scope(pc)
        cid = ids.chart_id(proj_id, scope)
        fid = ids.file_id(proj_id, _chart_file(pc.chart_dir))
        name = pc.chart.name if pc.chart else scope

        yield GraphNode("File", {"id": fid}, {"path": _chart_file(pc.chart_dir), "name": "Chart.yaml"})
        yield GraphEdge("HAS_FILE", "Project", {"id": proj_id}, "File", {"id": fid})
        yield GraphNode("Resource", {"id": cid}, {
            "name": name, "kind": "chart", "chart_dir": pc.chart_dir,
            "version": pc.chart.version if pc.chart else "",
            "app_version": pc.chart.app_version if pc.chart else "",
            "resolved": True,
        })
        yield GraphEdge("HAS_RESOURCE", "File", {"id": fid}, "Resource", {"id": cid})

        for dep in (pc.chart.dependencies if pc.chart else ()):
            target = by_name.get(dep)
            if target is None:  # external dependency -> a project-scoped placeholder chart
                target = ids.dep_id(proj_id, dep)
                yield GraphNode("Resource", {"id": target}, {"name": dep, "kind": "chart", "resolved": False})
            if target != cid:
                yield GraphEdge("DEPENDS_ON", "Resource", {"id": cid}, "Resource", {"id": target})

        for w in pc.workloads:
            wid = ids.workload_id(proj_id, scope, w.template, w.name)
            yield GraphNode("Service", {"id": wid}, {
                "name": w.name, "kind": w.kind, "template": w.template, "chart_dir": pc.chart_dir,
            })
            yield GraphEdge("DEPLOYS", "Resource", {"id": cid}, "Service", {"id": wid})
            for ref in dict.fromkeys(w.images):
                yield image_node(ref)
                yield GraphEdge("RUNS_IMAGE", "Service", {"id": wid}, "Image", {"id": image_id(ref)})
