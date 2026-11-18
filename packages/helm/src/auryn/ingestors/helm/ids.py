"""Canonical id scheme for the Helm graph (ADR 0007 §2). A chart is a `Resource` (the release),
keyed by its chart DIRECTORY (`helm://<project>/<chart-dir>`); each workload its templates declare is a
`Service`, keyed `helm://<project>/<chart-dir>/<template>/<name>`. The shared `Image` hub keys via
`core.image_ref`, so a chart's workload that runs an image converges with the other producers."""

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


def chart_scope(chart_dir: str, chart_name: str) -> str:
    """The chart's id segment: its directory (relative, slash-normalized) when it lives in a subdir,
    else its Chart.yaml name -- so a chart at the repo ROOT still gets a non-empty, distinct scope and
    its `Resource` id can't collide with the `Project` id."""
    d = chart_dir.replace(chr(92), "/").strip("/")
    return d or chart_name or "chart"


def chart_id(proj_id: str, scope: str) -> str:
    return f"{proj_id}/{scope}"


def workload_id(proj_id: str, scope: str, template: str, name: str) -> str:
    return f"{chart_id(proj_id, scope)}/{template}/{name}"


def dep_id(proj_id: str, dep_name: str) -> str:
    # A dependency that resolves to an in-repo chart uses that chart's id; an EXTERNAL dependency (not
    # in the repo) gets a project-scoped placeholder keyed by name, so two charts depending on the same
    # external chart converge on one node. `_dep/` can't collide with a chart dir (it's reserved).
    return f"{proj_id}/_dep/{dep_name}"
