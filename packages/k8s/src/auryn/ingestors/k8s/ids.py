"""Canonical id scheme for the Kubernetes graph (ADR 0007 §2). A workload `Service` is keyed by its
cluster coordinates (`k8s://<project>/<namespace>/<kind>/<name>`); an `Ingress` route by its
namespace+host+path (one node per route, so routes diff cleanly on resync). The shared `Image` hub and
the route-keyed `APIEndpoint` key via the core helpers, so they converge with the other producers."""

from __future__ import annotations

from auryn.core.image_ref import image_id  # re-exported: the Image id lives in one place

__all__ = ["project_id", "file_id", "workload_id", "ingress_id", "image_id"]


def project_id(project_name: str) -> str:
    return f"k8s://{project_name}"


def file_id(proj_id: str, rel_path: str) -> str:
    rel = rel_path.replace(chr(92), "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return f"{proj_id}/file/{rel}"


def workload_id(proj_id: str, namespace: str, kind: str, name: str) -> str:
    return f"{proj_id}/{namespace}/{kind.lower()}/{name}"


def ingress_id(proj_id: str, namespace: str, host: str | None, route: str) -> str:
    return f"{proj_id}/{namespace}/ingress/{host or '_'}{route}"
