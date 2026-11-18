"""Canonical id scheme for the Helm graph (ADR 0007 §2). A chart is a `Resource` (the release),
keyed by its chart DIRECTORY (`helm://<project>/<chart-dir>`); each workload its templates declare is a
`Service`, keyed `helm://<project>/<chart-dir>/<template-path>/<kind>/<name>`. The shared `Image` hub
keys via `core.image_ref`, so a chart's workload that runs an image converges with the other producers."""

from __future__ import annotations

from auryn.core.image_ref import image_id  # re-exported

__all__ = ["project_id", "file_id", "chart_scope", "chart_id", "workload_id", "dep_id", "image_id"]


def project_id(project_name: str) -> str:
    return f"helm://{project_name}"


def file_id(proj_id: str, rel_path: str) -> str:
    rel = rel_path.replace(chr(92), "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return f"{proj_id}/file/{rel}"


def chart_scope(chart_dir: str) -> str:
    """The chart's id segment: its DIRECTORY (relative, slash-normalized) -- unique per chart, since one
    `Chart.yaml` anchors exactly one directory. The repo-ROOT chart (empty dir) uses the reserved `_root`
    segment, so its `Resource` id can't collide with the `Project` id nor with a sibling subchart's
    directory. Keying on the directory (never the Chart.yaml `name`) is what prevents a root chart whose
    name equals a sibling subdir from merging with that subchart."""
    d = chart_dir.replace(chr(92), "/").strip("/")
    return d or "_root"


def chart_id(proj_id: str, scope: str) -> str:
    return f"{proj_id}/{scope}"


def workload_id(proj_id: str, scope: str, template: str, kind: str, name: str) -> str:
    """Key a workload on its chart-relative TEMPLATE PATH (not just the basename, so `a/deploy.yaml` and
    `b/deploy.yaml` stay distinct) + the workload KIND + name. With the parser disambiguating same
    (kind, name) workloads within one file, two workloads can never share an id."""
    t = template.replace(chr(92), "/").strip("/")
    return f"{chart_id(proj_id, scope)}/{t}/{kind.lower()}/{name}"


def dep_id(proj_id: str, dep_name: str) -> str:
    # A dependency that resolves to an in-repo chart uses that chart's id; an EXTERNAL dependency (not
    # in the repo) gets a project-scoped placeholder keyed by name, so two charts depending on the same
    # external chart converge on one node. `_dep/` can't collide with a chart dir (it's reserved).
    return f"{proj_id}/_dep/{dep_name}"
