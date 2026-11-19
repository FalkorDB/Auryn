"""Canonical id scheme for the Helm graph (ADR 0007 §2). A chart is a `Resource` (the release), keyed
by its chart DIRECTORY (`helm://<project>/<chart-dir>`); its notable config values are `ConfigValue`
nodes keyed owner-scoped (`<resource-id>/env/<NAME>`, the tier-wide ConfigValue scheme, ADR line 72).
The shared `Image` hub keys via `core.image_ref`, so a chart's image converges with the other
producers. v1 emits no `Service` (a chart's workloads need a render -- the future k8s arm)."""

from __future__ import annotations

from auryn.core.image_ref import image_id  # re-exported: the Image id lives in one place

__all__ = ["project_id", "file_id", "chart_scope", "chart_id", "dep_id", "config_value_id", "image_id"]


def project_id(project_name: str) -> str:
    return f"helm://{project_name}"


def file_id(proj_id: str, rel_path: str) -> str:
    rel = rel_path.replace(chr(92), "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return f"{proj_id}/file/{rel}"


def chart_scope(chart_dir: str) -> str:
    """The chart's id segment: its DIRECTORY (relative, slash-normalized). Keying on the directory --
    never the Chart.yaml `name` -- is what stops a root chart whose name equals a sibling subdir from
    merging with that subchart (two charts in two dirs always get two scopes). The repo-ROOT chart
    (empty dir) uses the reserved `_root` segment. `_root` and the `_dep/` dependency namespace are
    reserved: `git_sync._collect_charts` skips a real chart that would land on either, so a path can
    never collide with them (see `dep_id`)."""
    return chart_dir.replace(chr(92), "/").strip("/") or "_root"


def chart_id(proj_id: str, scope: str) -> str:
    return f"{proj_id}/{scope}"


def dep_id(proj_id: str, dep_name: str) -> str:
    # A dependency that resolves to an in-repo chart uses that chart's id; an EXTERNAL dependency (not
    # in the repo) gets a project-scoped placeholder keyed by name, so two charts depending on the same
    # external chart converge on one node. The `_dep/` namespace is RESERVED -- chart discovery
    # (git_sync._collect_charts) skips any real chart whose directory is `_dep`/`_dep/...`, so a
    # placeholder can never collide with a chart `Resource`.
    return f"{proj_id}/_dep/{dep_name}"


def config_value_id(owner_id: str, name: str) -> str:
    # The tier-wide ConfigValue scheme is `<owner-id>/env/<NAME>` (ADR 0007 line 72); the owner is the
    # chart's `Resource`, so the value is scoped to -- and GC'd with -- its chart.
    return f"{owner_id}/env/{name}"
