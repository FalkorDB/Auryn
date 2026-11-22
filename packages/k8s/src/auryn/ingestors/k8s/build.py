"""Turn parsed k8s manifests into the deployment-tier graph (ADR 0007): File -> Service (workload),
each workload RUNS_IMAGE the shared `Image` hub; File -> Ingress for each routing rule.

The Ingress backend names a k8s networking `Service`, whose selector picks the workload it fronts. We
resolve `Ingress backend -> Service(selector) -> Workload` here (the producer sees every manifest), so
the `Ingress` node records the resolved `backend_workload` id for the stitch. A conventional
`backend == workload name` fallback covers manifests with no Service (or no selector match). The
build->serve join (route-keyed APIEndpoint, SERVES) is the `deploy-k8s` stitch's job."""

from __future__ import annotations

import posixpath
from collections.abc import Iterator
from dataclasses import dataclass

from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.core.http_seam import normalize_route
from auryn.core.image_ref import image_id, image_node
from auryn.ingestors.k8s import ids
from auryn.ingestors.k8s.model import ParsedManifests


@dataclass(frozen=True)
class _W:
    namespace: str
    kind: str
    name: str
    labels: dict
    wid: str


def _labels_match(selector: tuple[tuple[str, str], ...], labels: dict) -> bool:
    """A selector matches a workload iff every selector label is present (and equal) on the workload."""
    return bool(selector) and all(labels.get(k) == v for k, v in selector)


def _resolve_backend(ing, services: dict, workloads: list[_W]) -> str | None:
    """`Ingress backend -> k8s Service(selector) -> Workload`, preferring a Deployment. Falls back to a
    workload whose NAME equals the backend (the conventional Service-named-like-its-Deployment case, or
    a manifest with no Service object)."""
    if not ing.backend_service:
        return None
    ns_workloads = [w for w in workloads if w.namespace == ing.namespace]
    selector = services.get((ing.namespace, ing.backend_service))
    if selector:
        matches = [w for w in ns_workloads if _labels_match(selector, w.labels)]
        if matches:
            matches.sort(key=lambda w: (0 if w.kind == "Deployment" else 1, w.name))
            return matches[0].wid
    byname = sorted((w for w in ns_workloads if w.name == ing.backend_service),
                    key=lambda w: (0 if w.kind == "Deployment" else 1, w.name))
    return byname[0].wid if byname else None


def build_graph(files: list[tuple[str, ParsedManifests]], proj_id: str) -> Iterator[GraphNode | GraphEdge]:
    # Pre-pass across ALL files: index networking Services (by ns+name -> selector) and workloads, so
    # an Ingress can resolve its backend to a workload that may live in another file.
    services: dict[tuple[str, str], tuple] = {}
    workloads: list[_W] = []
    for _rel, parsed in files:
        for s in parsed.services:
            services[(s.namespace, s.name)] = s.selector
        for w in parsed.workloads:
            workloads.append(_W(w.namespace, w.kind, w.name, dict(w.labels),
                                ids.workload_id(proj_id, w.namespace, w.kind, w.name)))

    for rel_path, parsed in files:
        fid = ids.file_id(proj_id, rel_path)
        yield GraphNode("File", {"id": fid}, {"path": rel_path, "name": posixpath.basename(rel_path)})
        yield GraphEdge("HAS_FILE", "Project", {"id": proj_id}, "File", {"id": fid})

        for w in parsed.workloads:
            sid = ids.workload_id(proj_id, w.namespace, w.kind, w.name)
            yield GraphNode("Service", {"id": sid}, {
                "name": w.name, "kind": w.kind, "namespace": w.namespace,
                "labels": [f"{k}={v}" for k, v in w.labels],
            })
            yield GraphEdge("HAS_SERVICE", "File", {"id": fid}, "Service", {"id": sid})
            for ref in dict.fromkeys(w.images):
                yield image_node(ref)
                yield GraphEdge("RUNS_IMAGE", "Service", {"id": sid}, "Image", {"id": image_id(ref)})

        for ing in parsed.ingresses:
            route = normalize_route(ing.path)  # the path-matching key (shared with the http seam)
            iid = ids.ingress_id(proj_id, ing.namespace, ing.ingress_name, ing.host, ing.path)
            yield GraphNode("Ingress", {"id": iid}, {
                "name": ing.ingress_name,
                "namespace": ing.namespace,
                "host": ing.host,
                "path": ing.path,              # RAW, case-preserved
                "route": route,                # normalized route for the APIEndpoint key
                "path_type": ing.path_type,
                "is_default": ing.is_default,
                "backend_service": ing.backend_service or "",
                "backend_workload": _resolve_backend(ing, services, workloads) or "",
            })
            yield GraphEdge("HAS_INGRESS", "File", {"id": fid}, "Ingress", {"id": iid})

