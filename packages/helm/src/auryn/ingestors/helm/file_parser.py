"""Parse a Helm chart (render-free) -> `ParsedChart`. Pure, no graph.

`Chart.yaml` and `values.yaml` are concrete YAML (PyYAML). The `templates/*.yaml` are Go-templated
k8s manifests -- NOT valid YAML -- so they're scanned textually per `---` document: a top-level
`kind:` identifies a workload, its container `image:` refs are resolved against `values.yaml` (see
`values.resolve_image`), and its name is read from `metadata.name` (falling back to the template file
stem when the name is itself templated). Best-effort and never fatal."""

from __future__ import annotations

import posixpath
import re

import yaml

from auryn.ingestors.helm.model import Chart, ParsedChart, Workload
from auryn.ingestors.helm.values import resolve_image

# k8s kinds that run containers -- a template declaring one is a deployable Workload (-> Service).
_WORKLOAD_KINDS = frozenset({
    "Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "ReplicationController",
    "Job", "CronJob", "Pod", "Rollout",
})


def _safe_yaml(data: str | bytes) -> dict:
    """A concrete YAML document (Chart.yaml / values.yaml) as a dict; {} on anything unexpected."""
    try:
        doc = yaml.safe_load(data)
    except Exception:
        return {}
    return doc if isinstance(doc, dict) else {}


def parse_chart_yaml(data: str | bytes) -> Chart | None:
    doc = _safe_yaml(data)
    name = doc.get("name")
    if not isinstance(name, str) or not name:
        return None
    deps: list[str] = []
    for d in doc.get("dependencies", []) or []:
        if isinstance(d, dict) and isinstance(d.get("name"), str):
            deps.append(d["name"])
    return Chart(
        name=name,
        version=str(doc.get("version", "")),
        app_version=str(doc.get("appVersion", "")),
        dependencies=tuple(dict.fromkeys(deps)),
    )


def load_values(data: str | bytes) -> dict:
    return _safe_yaml(data)


def _documents(text: str):
    """Split a template file into its `---`-separated YAML documents (textually -- the content is
    templated, so we never YAML-parse it)."""
    for doc in re.split(r"(?m)^---\s*$", text):
        if doc.strip():
            yield doc


def _kind(doc: str) -> str | None:
    m = re.search(r"(?m)^kind:\s*[\"']?([A-Za-z][A-Za-z0-9]*)", doc)
    return m.group(1) if m else None


def _workload_name(doc: str, template_stem: str) -> str:
    """`metadata.name` when it is a concrete literal; otherwise (a `{{ include ... }}` name, or none)
    the template file stem -- a stable, readable fallback."""
    meta = re.search(r"(?m)^metadata:\s*$", doc)
    if meta:
        nm = re.search(r"(?m)^\s+name:\s*(.+?)\s*$", doc[meta.end():])
        if nm:
            val = nm.group(1).strip().strip("\"'")
            if val and "{{" not in val:
                return val
    return template_stem


def _images(doc: str, values: dict) -> tuple[str, ...]:
    out: dict[str, None] = {}
    for m in re.finditer(r"(?m)^\s*-?\s*image:\s*(.+?)\s*$", doc):
        resolved = resolve_image(m.group(1).strip(), values)
        if resolved:
            out.setdefault(resolved, None)
    return tuple(out)


def _parse_template(rel_name: str, text: str, values: dict) -> list[Workload]:
    stem = posixpath.splitext(posixpath.basename(rel_name))[0]
    out: list[Workload] = []
    for doc in _documents(text):
        kind = _kind(doc)
        if kind in _WORKLOAD_KINDS:
            out.append(Workload(kind=kind, name=_workload_name(doc, stem), template=stem, images=_images(doc, values)))
    return out


def parse_chart(chart_dir: str, chart_yaml, values_yaml, templates: list[tuple[str, str]]) -> ParsedChart:
    """Assemble a `ParsedChart` from a chart's raw files. `templates` is a list of `(relative-name,
    text)` for the chart's `templates/*.yaml`. `chart_yaml`/`values_yaml` may be None/empty."""
    chart = parse_chart_yaml(chart_yaml) if chart_yaml else None
    values = load_values(values_yaml) if values_yaml else {}
    workloads: list[Workload] = []
    for rel_name, text in templates:
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="replace")
        workloads.extend(_parse_template(rel_name, text, values))
    return ParsedChart(chart_dir=chart_dir, chart=chart, workloads=tuple(workloads))
