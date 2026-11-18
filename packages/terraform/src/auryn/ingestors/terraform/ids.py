"""Canonical id scheme for the Terraform graph (ADR 0007 §2). A `Resource` is keyed by its Terraform
address within the project (`terraform://<project>/<address>`). The shared `Image` hub keys via
`core.image_ref`, so a resource that runs an image converges with the other producers."""

from __future__ import annotations

from auryn.core.image_ref import image_id  # re-exported

__all__ = ["project_id", "file_id", "resource_id", "database_id", "registry_id", "image_id"]


def project_id(project_name: str) -> str:
    return f"terraform://{project_name}"


def file_id(proj_id: str, rel_path: str) -> str:
    rel = rel_path.replace(chr(92), "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return f"{proj_id}/file/{rel}"


def _module_prefix(proj_id: str, module_dir: str) -> str:
    # A Terraform MODULE is a directory: resources in `env/dev` and `env/prod` are distinct configs, so
    # the module dir is part of the id -- otherwise same-named resources across modules collide.
    return f"{proj_id}/{module_dir}" if module_dir else proj_id


def resource_id(proj_id: str, module_dir: str, address: str) -> str:
    return f"{_module_prefix(proj_id, module_dir)}/{address}"


def database_id(proj_id: str, module_dir: str, address: str) -> str:
    return f"{_module_prefix(proj_id, module_dir)}/database/{address}"


def registry_id(proj_id: str, module_dir: str, address: str) -> str:
    return f"{_module_prefix(proj_id, module_dir)}/registry/{address}"
