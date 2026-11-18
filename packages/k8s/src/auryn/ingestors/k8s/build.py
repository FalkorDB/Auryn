"""Turn parsed k8s manifests into the deployment-tier graph (ADR 0007): File -> Service (workload),
each workload RUNS_IMAGE the shared `Image` hub; File -> Ingress for each routing rule. The
build->serve join (Ingress route -> APIEndpoint, backing workload -> SERVES it) is the `deploy-k8s`
stitch's job, reading the `Ingress` nodes emitted here.

Emits `Image` hub nodes `resolved:false`; the stitch flips an image internal once a `BUILDS_IMAGE`
(from docker/compose) proves it built. Emits no cross-artifact edge."""

from __future__ import annotations

import posixpath
from collections.abc import Iterator

from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.core.http_seam import normalize_route
from auryn.core.image_ref import image_id, parse_image_ref
from auryn.ingestors.k8s import ids
from auryn.ingestors.k8s.model import ParsedManifests


def build_graph(files: list[tuple[str, ParsedManifests]], proj_id: str) -> Iterator[GraphNode | GraphEdge]:
    for rel_path, parsed in files:
        fid = ids.file_id(proj_id, rel_path)
        yield GraphNode("File", {"id": fid}, {"path": rel_path, "name": posixpath.basename(rel_path)})
        yield GraphEdge("HAS_FILE", "Project", {"id": proj_id}, "File", {"id": fid})

        for w in parsed.workloads:
            sid = ids.workload_id(proj_id, w.namespace, w.kind, w.name)
            yield GraphNode("Service", {"id": sid},
                            {"name": w.name, "kind": w.kind, "namespace": w.namespace})
            yield GraphEdge("HAS_SERVICE", "File", {"id": fid}, "Service", {"id": sid})
            for ref in dict.fromkeys(w.images):  # dedup, keep order
                yield from _emit_image(ref)
                yield GraphEdge("RUNS_IMAGE", "Service", {"id": sid}, "Image", {"id": image_id(ref)})

        for ing in parsed.ingresses:
            route = normalize_route(ing.path)
            iid = ids.ingress_id(proj_id, ing.namespace, ing.host, route)
            yield GraphNode("Ingress", {"id": iid}, {
                "namespace": ing.namespace,
                "host": ing.host,
                "route": route,
                "backend_service": ing.backend_service or "",
            })
            yield GraphEdge("HAS_INGRESS", "File", {"id": fid}, "Ingress", {"id": iid})


def _emit_image(ref: str) -> Iterator[GraphNode]:
    r = parse_image_ref(ref)
    yield GraphNode(
        "Image",
        {"id": image_id(ref)},
        {
            "ref": r.normalized,
            "registry": r.registry,
            "repository": r.repository,
            "tag": r.tag,
            "digest": r.digest,
            "resolved": False,
            "origin": "registry",
            **({"raw": r.raw} if r.unresolved else {}),
        },
    )
