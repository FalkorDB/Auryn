"""Canonical id scheme for the Dockerfile graph (ADR 0007 §2), mirroring the other ingestors'
shape (`<lang>://<project>/...`) so one graph holds every source with interchangeable ids.

Docker-specific choices:
- A `BuildStage` is keyed off the *file* that declares it plus its stage name (or index when the
  stage is unnamed) -- `docker://<project>/file/<path>#<stage>`.
- An `Image` is keyed by its project-INDEPENDENT normalized reference (`image://<ref>`, see
  `image_ref.image_id`): it is the hub node a build and a run converge on across repos, so -- like a
  cross-repo `extern://` placeholder -- it must NOT be scoped to one project.
"""

from __future__ import annotations

from auryn.ingestors.docker.image_ref import image_id  # re-exported: the Image id lives in one place

__all__ = ["project_id", "file_id", "stage_id", "image_id"]


def project_id(project_name: str) -> str:
    return f"docker://{project_name}"


def file_id(proj_id: str, rel_path: str) -> str:
    return f"{proj_id}/file/{rel_path.replace(chr(92), '/').lstrip('./')}"


def stage_id(file_id_: str, stage_key: str) -> str:
    """`stage_key` is the `AS` name, or the stringified 0-based index for an unnamed stage."""
    return f"{file_id_}#{stage_key}"
