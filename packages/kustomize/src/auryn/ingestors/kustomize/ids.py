"""Canonical id scheme for the Kustomize graph (ADR 0007 §2). A kustomization is a `Resource` (an
overlay/base), keyed by its DIRECTORY (`kustomize://<project>/<kustomization-dir>`); the repo-root
kustomization uses the reserved `_root` segment. The shared `Image` hub keys via `core.image_ref`, so
an overlay's `images:` transformer converges with the other producers."""

from __future__ import annotations

from auryn.core.image_ref import image_id  # re-exported: the Image id lives in one place

__all__ = ["project_id", "file_id", "kustomization_scope", "kustomization_id", "image_id"]


def project_id(project_name: str) -> str:
    return f"kustomize://{project_name}"


def file_id(proj_id: str, rel_path: str) -> str:
    rel = rel_path.replace(chr(92), "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return f"{proj_id}/file/{rel}"


def kustomization_scope(kustomization_dir: str) -> str:
    """The id segment: the kustomization's DIRECTORY (relative, slash-normalized), or the reserved
    `_root` for a kustomization at the repo root -- so its `Resource` id can't collide with the
    `Project` id. Keying on the directory (never a name) makes two kustomizations always distinct."""
    return kustomization_dir.replace(chr(92), "/").strip("/") or "_root"


def kustomization_id(proj_id: str, scope: str) -> str:
    return f"{proj_id}/{scope}"
