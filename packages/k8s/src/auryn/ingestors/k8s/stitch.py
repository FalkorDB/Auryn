"""The `deploy-k8s` stitch -- joins cluster routing to the HTTP seam (ADR 0007 §4 + ADR 0006).

An Ingress declares an external route and the k8s Service it routes to. This pass keys each route to a
route-keyed `APIEndpoint` -- the SAME node a code route handler `EXPOSES_ENDPOINT` (via
`core.http_seam`) -- and links the backing workload to it with `SERVES`:

    GET /api  ->  Ingress  -SERVES->  APIEndpoint  <-EXPOSES_ENDPOINT-  handler()

so the chain reaches from an external URL to the function that serves it. The backing workload is
matched by the Ingress backend's Service name within the same namespace+project (k8s Services and their
Deployments conventionally share a name). Also runs the shared `core.deploy` image lifecycle, so a
k8s-only repo still gets image `resolved`/GC.

Graph-only + idempotent + per-project: it owns the `k8s-stitch` SERVES layer, rebuilt each run."""

from __future__ import annotations

import logging

from auryn.core import deploy
from auryn.core.graph_client import GraphWriter
from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.core.http_seam import endpoint_placeholder_key

logger = logging.getLogger("auryn.deploy-k8s")


def run(writer: GraphWriter, repo, target_sha: str) -> dict | None:
    return stitch(writer)


def _project_of(node_id: str) -> str:
    if "://" not in node_id:
        return ""
    return node_id.split("://", 1)[1].split("/", 1)[0]


def _is_empty_graph(exc: Exception) -> bool:
    return "empty key" in str(exc).lower()


def stitch(writer: GraphWriter) -> dict | None:
    try:
        ingresses = writer.ro_query(
            "MATCH (ing:Ingress) RETURN ing.id, ing.namespace, ing.route, ing.backend_service"
        ).result_set
    except Exception as e:
        if _is_empty_graph(e):
            return None
        raise

    _clear_prior_layer(writer)

    endpoints: dict[str, GraphNode] = {}
    edges: list[GraphEdge] = []
    for iid, ns, route, backend in ingresses:
        proj = _project_of(str(iid))
        ep_id = endpoint_placeholder_key("ANY", str(route))
        # MERGE the shared route-keyed APIEndpoint WITHOUT `resolved` -- never clobber a handler's
        # resolved flag; a fresh one is left for the http-endpoints hook to resolve.
        endpoints.setdefault(ep_id, GraphNode(
            "APIEndpoint", {"id": ep_id},
            {"route": str(route), "http_method": "ANY", "provenance": "http-derived"},
        ))
        if backend:
            rows = writer.ro_query(
                "MATCH (s:Service) WHERE s.name = $n AND s.id STARTS WITH $pfx RETURN s.id LIMIT 1",
                {"n": str(backend), "pfx": f"k8s://{proj}/{ns}/"},
            ).result_set
            if rows:
                edges.append(GraphEdge("SERVES", "Service", {"id": str(rows[0][0])},
                                       "APIEndpoint", {"id": ep_id}, {"provenance": "k8s-stitch"}))

    if endpoints:
        writer.merge_nodes(list(endpoints.values()))
    if edges:
        writer.merge_edges(edges)
    deploy.recompute_image_resolved(writer)
    orphans = deploy.gc_orphan_images(writer)

    if not edges and not orphans:
        return None
    report = {"serves_edges": len(edges), "endpoints": len(endpoints), "orphans_removed": orphans}
    logger.info("deploy-k8s: %d SERVES edge(s) -> %d route endpoint(s), %d orphan image(s) GC'd",
                len(edges), len(endpoints), orphans)
    return report


def _clear_prior_layer(writer: GraphWriter) -> None:
    """Drop this hook's own SERVES edges so a re-run rebuilds its layer (idempotent). Only
    `k8s-stitch`-provenanced edges are touched."""
    writer.query("MATCH ()-[r:SERVES]->() WHERE r.provenance = 'k8s-stitch' DELETE r")
