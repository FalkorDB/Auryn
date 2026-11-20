"""Canonical id scheme for the CI graph (ADR 0007 §2). A workflow is a `Resource` (a CI pipeline),
keyed by its file path (`ci://<project>/<workflow-path>`). The shared `Image` hub keys via
`core.image_ref`, so an image a workflow builds converges with the image other producers run."""

from __future__ import annotations

from auryn.core.image_ref import image_id  # re-exported: the Image id lives in one place

__all__ = ["project_id", "file_id", "workflow_id", "image_id"]


def project_id(project_name: str) -> str:
    return f"ci://{project_name}"


def file_id(proj_id: str, rel_path: str) -> str:
    rel = rel_path.replace(chr(92), "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return f"{proj_id}/file/{rel}"


def workflow_id(proj_id: str, rel_path: str) -> str:
    rel = rel_path.replace(chr(92), "/").strip("/")
    return f"{proj_id}/{rel}"
