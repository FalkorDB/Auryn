"""Parsed Helm model (engine-agnostic). `file_parser.parse_chart` produces a `ParsedChart` per chart
directory; `build.build_graph` turns them into graph nodes/edges."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Chart:
    """A Helm chart, from its `Chart.yaml`. `dependencies` are the names of the charts it depends on
    (`dependencies:` in Chart.yaml / the legacy `requirements.yaml`) -- they become DEPENDS_ON edges,
    resolved to an in-repo chart when one matches, else a placeholder."""

    name: str
    version: str = ""
    app_version: str = ""
    dependencies: tuple[str, ...] = ()


@dataclass(frozen=True)
class Workload:
    """A deployable workload found in a `templates/*.yaml` file (a `kind:` of Deployment, StatefulSet,
    DaemonSet, Job, CronJob, ...). `name` is the manifest's resolved name, or -- when it is templated
    (`{{ include "chart.fullname" . }}`) and can't be resolved statically -- the template file stem
    (a second+ workload of the same kind+name within one file is suffixed `-2`, `-3`, ... to stay
    distinct). `template` is the template file's path RELATIVE TO THE CHART DIR (e.g. `templates/deploy.yaml`
    -- part of the id, so nested templates sharing a basename don't collide). `images` are the container
    image refs it runs, resolved against `values.yaml` (the hub seam); only fully-resolved refs are kept."""

    kind: str
    name: str
    template: str
    images: tuple[str, ...] = ()


@dataclass(frozen=True)
class ParsedChart:
    """One Helm chart: its `Chart.yaml` metadata (or None if unreadable) and the workloads its
    templates declare. `chart_dir` is the chart's directory, relative to the repo root (the `Chart.yaml`
    anchor) -- the scope of every id this chart contributes."""

    chart_dir: str
    chart: Chart | None = None
    workloads: tuple[Workload, ...] = ()
