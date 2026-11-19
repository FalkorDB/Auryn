"""Turn parsed Helm charts into the deployment-tier graph (ADR 0007). STATIC, render-free:

    Project -HAS_FILE-> File (Chart.yaml, values.yaml)
    File (Chart.yaml) -HAS_RESOURCE-> Resource          (the chart/release, kind="release")
    Resource -DEPENDS_ON-> Resource                     (a chart dependency; in-repo chart else placeholder)
    Resource -RUNS_IMAGE-> Image                        (each container image declared in values.yaml -> the hub)
    Resource -MOUNTS-> ConfigValue                      (each notable static config value from values.yaml)

Emits no cross-artifact edge beyond the shared `Image` hub (`resolved:false` placeholders); the
`deploy-helm` enrichment runs the shared image lifecycle. No `Service`/`DEPLOYS`: a chart's workloads
live in the Go-templated `templates/*.yaml`, which v1 does NOT render (ADR 0007 line 225) -- so the
chart's images attach to the release `Resource` via `RUNS_IMAGE` (ADR §3 allows a `Resource` source).

A dependency naming an in-repo chart resolves to that chart's real `Resource`; an external dependency
(not in the repo) falls back to a project-scoped `resolved:false` placeholder (the ADR 0004 pattern)."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterator

from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.core.image_ref import image_id, image_node
from auryn.ingestors.helm import ids
from auryn.ingestors.helm.model import ParsedChart

# A config KEY whose name signals a sensitive value (ADR 0007: a sensitive entry is redacted, never
# stored). Matched on the dotted name, so `database.password` / `auth.apiKey` are caught at any depth.
# The final branch catches a bare `key`/`keys` SEGMENT (`tls.key`, `api.key`, `signing_key` -> a private
# key) while not firing on `monkey`/`keyboard`/`publicKey` (a public key isn't a secret).
_SENSITIVE = re.compile(
    r"(?i)(password|passwd|secret|token|credential|api[_-]?key|access[_-]?key|private[_-]?key"
    r"|(?:^|[._-])keys?(?:[._-]|$))"
)


def _is_sensitive(name: str) -> bool:
    return bool(_SENSITIVE.search(name))


def _scope(pc: ParsedChart) -> str:
    return ids.chart_scope(pc.chart_dir)


def _chart_file(chart_dir: str) -> str:
    return posixpath.join(chart_dir, "Chart.yaml") if chart_dir else "Chart.yaml"


def _values_file(chart_dir: str) -> str:
    return posixpath.join(chart_dir, "values.yaml") if chart_dir else "values.yaml"


def build_graph(charts: list[ParsedChart], proj_id: str) -> Iterator[GraphNode | GraphEdge]:
    # Index chart NAME -> its Resource id, so a `dependencies:` entry naming an in-repo chart resolves
    # to a real DEPENDS_ON (an external dependency falls back to a project-scoped placeholder). Only an
    # UNAMBIGUOUS name (exactly one in-repo chart) resolves: a name shared by several charts (vendored
    # subcharts, multiple versions) stays a placeholder rather than resolving arbitrarily to one.
    name_ids: dict[str, list[str]] = {}
    for pc in charts:
        if pc.chart:
            name_ids.setdefault(pc.chart.name, []).append(ids.chart_id(proj_id, _scope(pc)))
    by_name: dict[str, str] = {n: v[0] for n, v in name_ids.items() if len(v) == 1}

    for pc in charts:
        scope = _scope(pc)
        cid = ids.chart_id(proj_id, scope)
        chart_rel = _chart_file(pc.chart_dir)
        fid = ids.file_id(proj_id, chart_rel)
        name = pc.chart.name if pc.chart else scope

        yield GraphNode("File", {"id": fid}, {"path": chart_rel, "name": "Chart.yaml"})
        yield GraphEdge("HAS_FILE", "Project", {"id": proj_id}, "File", {"id": fid})
        yield GraphNode("Resource", {"id": cid}, {
            "name": name, "kind": "release", "chart_dir": pc.chart_dir,
            "version": pc.chart.version if pc.chart else "",
            "app_version": pc.chart.app_version if pc.chart else "",
            "chart_type": pc.chart.chart_type if pc.chart else "",
            "resolved": pc.chart is not None,  # a nameless/unreadable Chart.yaml isn't a valid chart
        })
        yield GraphEdge("HAS_RESOURCE", "File", {"id": fid}, "Resource", {"id": cid})

        if pc.has_values:
            values_rel = _values_file(pc.chart_dir)
            vfid = ids.file_id(proj_id, values_rel)
            yield GraphNode("File", {"id": vfid}, {"path": values_rel, "name": "values.yaml"})
            yield GraphEdge("HAS_FILE", "Project", {"id": proj_id}, "File", {"id": vfid})

        for dep in (pc.chart.dependencies if pc.chart else ()):
            target = by_name.get(dep.name)
            if target is None:  # external dependency -> a project-scoped placeholder chart (unresolved)
                target = ids.dep_id(proj_id, dep.name)
                yield GraphNode("Resource", {"id": target}, {
                    "name": dep.name, "kind": "chart", "resolved": False,
                    "version": dep.version, "repository": dep.repository,
                })
            if target != cid:
                yield GraphEdge("DEPENDS_ON", "Resource", {"id": cid}, "Resource", {"id": target})

        for ref in pc.images:
            yield image_node(ref)
            yield GraphEdge("RUNS_IMAGE", "Resource", {"id": cid}, "Image", {"id": image_id(ref)})

        for cv_name, cv_value in pc.config_values:
            vid = ids.config_value_id(cid, cv_name)
            # Redact a sensitive value (a `*password*`/`*token*`/... key): the graph, viewer and MCP
            # reads surface ConfigValue.value, so a credential in values.yaml must never be stored.
            sensitive = _is_sensitive(cv_name)
            yield GraphNode("ConfigValue", {"id": vid}, {
                "name": cv_name, "value": "[redacted]" if sensitive else cv_value,
                "sensitive": sensitive, "chart_dir": pc.chart_dir,
            })
            # MOUNTS is the tier's config-wiring edge (ADR 0007 line 107); v1 helm has no Service, so the
            # release Resource is the owner -- mirroring how ADR §3 generalized RUNS_IMAGE/DEPENDS_ON to
            # a Resource source. Reuses the contract edge rather than inventing one.
            yield GraphEdge("MOUNTS", "Resource", {"id": cid}, "ConfigValue", {"id": vid})
