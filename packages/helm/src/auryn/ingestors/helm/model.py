"""Parsed Helm model (engine-agnostic). `file_parser.parse_chart` produces a `ParsedChart` per chart
directory; `build.build_graph` turns them into graph nodes/edges.

v1 is STATIC and render-free: only a chart's YAML-valid files (`Chart.yaml` + `values.yaml`) are read.
The Go-templated `templates/*.yaml` are NOT parsed -- they are not valid YAML, and resolving the
workloads they declare needs a `helm template` render (the future k8s arm, ADR 0007 line 225)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Dependency:
    """One entry of a chart's `dependencies:` (Chart.yaml / the legacy requirements.yaml). `version`
    and `repository` are kept for the placeholder `Resource` a not-in-repo dependency becomes."""

    name: str
    version: str = ""
    repository: str = ""


@dataclass(frozen=True)
class Chart:
    """A Helm chart, from its `Chart.yaml`. `chart_type` is Chart.yaml's `type:` (`application` /
    `library`). `dependencies` become DEPENDS_ON edges -- resolved to an in-repo chart when one matches
    by name, else a `resolved:false` placeholder `Resource` (the ADR 0004 reference pattern)."""

    name: str
    version: str = ""
    app_version: str = ""
    chart_type: str = ""
    dependencies: tuple[Dependency, ...] = ()


@dataclass(frozen=True)
class ParsedChart:
    """One Helm chart: its `Chart.yaml` metadata (or None if unreadable), the container image refs its
    `values.yaml` declares (the hub seam), and its notable static config values. `chart_dir` is the
    chart's directory relative to the repo root (the `Chart.yaml` anchor) -- the scope of every id this
    chart contributes. `has_values` records whether a `values.yaml` was present (so its File node is
    emitted only when it exists). v1 emits NO `Service`: a chart's workloads live in the Go-templated
    `templates/*.yaml`, which can't be resolved statically without a render (ADR 0007 line 225)."""

    chart_dir: str
    chart: Chart | None = None
    images: tuple[str, ...] = ()
    config_values: tuple[tuple[str, str], ...] = ()  # (dotted-name, value) scalar leaves (bounded depth)
    has_values: bool = False
