"""Canonical id scheme for the Compose graph (ADR 0007 §2), mirroring the other producers
(`<source>://<project>/...`). A `Service` is keyed off the compose *file* that declares it plus its
service name -- `compose://<project>/file/<path>#<service>` -- so two compose files in one repo keep
their same-named services distinct. The shared `Image` hub keys via `core.image_ref.image_id`, the
same normalization every deployment-tier producer uses."""

from __future__ import annotations

from auryn.core.image_ref import image_id  # re-exported: the Image id lives in one place

__all__ = ["project_id", "file_id", "service_id", "image_id"]


def project_id(project_name: str) -> str:
    return f"compose://{project_name}"


def file_id(proj_id: str, rel_path: str) -> str:
    rel = rel_path.replace(chr(92), "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return f"{proj_id}/file/{rel}"


def service_id(file_id_: str, name: str) -> str:
    return f"{file_id_}#{name}"
