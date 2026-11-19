"""Parse a Helm chart's STATIC, YAML-valid files -> `ParsedChart`. Pure, no graph.

v1 reads only `Chart.yaml` (chart name/version/type + `dependencies:`) and `values.yaml` (container
image refs + notable config values). The Go-templated `templates/*.yaml` are deliberately NOT parsed:
they are not valid YAML, and the workloads they declare can't be resolved without a `helm template`
render (the future k8s arm, ADR 0007 line 225). Best-effort and never fatal: a malformed file yields
an empty/None component rather than raising."""

from __future__ import annotations

import yaml

from auryn.ingestors.helm.model import Chart, Dependency, ParsedChart
from auryn.ingestors.helm.values import extract_config_values, extract_images


def _safe_yaml(data: str | bytes) -> dict:
    """A concrete YAML document (Chart.yaml / values.yaml) as a dict; {} on anything unexpected."""
    try:
        doc = yaml.safe_load(data)
    except Exception:
        return {}
    return doc if isinstance(doc, dict) else {}


def parse_chart_yaml(data: str | bytes) -> Chart | None:
    """`Chart.yaml` -> `Chart`, or None when it has no `name` (not a usable chart definition)."""
    doc = _safe_yaml(data)
    name = doc.get("name")
    if not isinstance(name, str) or not name:
        return None
    deps: list[Dependency] = []
    seen: set[str] = set()
    deps_raw = doc.get("dependencies")  # a malformed `dependencies: 42` must not raise (-> chart dropped)
    for d in deps_raw if isinstance(deps_raw, list) else []:
        if isinstance(d, dict) and isinstance(d.get("name"), str) and d["name"] not in seen:
            seen.add(d["name"])
            deps.append(Dependency(
                name=d["name"],
                version=str(d.get("version", "")),
                repository=str(d.get("repository", "")),
            ))
    return Chart(
        name=name,
        version=str(doc.get("version", "")),
        app_version=str(doc.get("appVersion", "")),
        chart_type=str(doc.get("type", "")),
        dependencies=tuple(deps),
    )


def load_values(data: str | bytes) -> dict:
    return _safe_yaml(data)


def parse_chart(chart_dir: str, chart_yaml, values_yaml) -> ParsedChart:
    """Assemble a `ParsedChart` from a chart's raw `Chart.yaml`/`values.yaml` bytes (either may be
    None). The chart's images and config values are read from `values.yaml`."""
    chart = parse_chart_yaml(chart_yaml) if chart_yaml else None
    has_values = values_yaml is not None
    values = load_values(values_yaml) if values_yaml else {}
    return ParsedChart(
        chart_dir=chart_dir,
        chart=chart,
        images=extract_images(values),
        config_values=extract_config_values(values),
        has_values=has_values,
    )
