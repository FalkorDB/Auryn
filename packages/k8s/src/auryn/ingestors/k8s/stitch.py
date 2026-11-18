"""The `deploy-k8s` stitch -- joins cluster routing to the HTTP seam (ADR 0007 §4 + ADR 0006).

An Ingress declares an external route and resolves (in the producer, via the k8s Service selector) to
the workload that serves it. This pass keys each route to a route-keyed `APIEndpoint` -- the SAME node
a code route handler `EXPOSES_ENDPOINT` -- and writes two edges:

    Service (workload) -SERVES-> APIEndpoint        (the workload serving the route; `host` on the edge)
    Ingress            -ROUTES-> APIEndpoint        (the host-specific routing object -> the route)

so a traversal from the external URL's Ingress reaches the endpoint and, via SERVES, the backing
workload (and through EXPOSES_ENDPOINT, the handler function). The APIEndpoint stays route-keyed so it
converges with code handlers; the HOST distinction lives on the Ingress node + the SERVES edge, not on
the shared endpoint. Also runs the shared `core.deploy` image lifecycle.

Idempotent: it owns the `k8s-stitch` SERVES/ROUTES layer, computed fresh then swapped in (prior edges
are cleared only AFTER the new set is built, so a mid-run failure can't drop live edges)."""

from __future__ import annotations

import logging

from auryn.core import deploy
from auryn.core.graph_client import GraphWriter
from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.core.http_seam import endpoint_placeholder_key

logger = logging.getLogger("auryn.deploy-k8s")


def run(writer: GraphWriter, repo, target_sha: str) -> dict | None:
    return stitch(writer)


def _is_empty_graph(exc: Exception) -> bool:
    return "empty key" in str(exc).lower()


def stitch(writer: GraphWriter) -> dict | None:
    try:
        ingresses = writer.ro_query(
            "MATCH (ing:Ingress) RETURN ing.id, ing.route, ing.host, ing.backend_workload"
        ).result_set
    except Exception as e:
        if _is_empty_graph(e):
            return None
        raise

    # Build the full new layer FIRST (all reads), then clear + write -- so a failure here never leaves
    # the prior SERVES/ROUTES deleted with no replacement.
    endpoints: dict[str, GraphNode] = {}
    edges: list[GraphEdge] = []
    for iid, route, host, backend in ingresses:
        route = str(route)
        ep_id = endpoint_placeholder_key("ANY", route)
        # MERGE the shared route-keyed APIEndpoint WITHOUT `resolved` -- never clobber a handler's flag.
        endpoints.setdefault(ep_id, GraphNode(
            "APIEndpoint", {"id": ep_id},
            {"route": route, "http_method": "ANY", "provenance": "http-derived"},
        ))
        edges.append(GraphEdge("ROUTES", "Ingress", {"id": str(iid)}, "APIEndpoint", {"id": ep_id},
                               {"provenance": "k8s-stitch", "host": host}))
        if backend:
            edges.append(GraphEdge("SERVES", "Service", {"id": str(backend)}, "APIEndpoint", {"id": ep_id},
                                   {"provenance": "k8s-stitch", "host": host}))

    _clear_prior_layer(writer)
    if endpoints:
        writer.merge_nodes(list(endpoints.values()))
    if edges:
        writer.merge_edges(edges)
    deploy.recompute_image_resolved(writer)
    orphans = deploy.gc_orphan_images(writer)

    serves = sum(1 for e in edges if e.rel_type == "SERVES")
    if not edges and not orphans:
        return None
    report = {"serves_edges": serves, "route_endpoints": len(endpoints), "orphans_removed": orphans}
    logger.info("deploy-k8s: %d SERVES + %d ROUTES -> %d endpoint(s), %d orphan image(s) GC'd",
                serves, len(edges) - serves, len(endpoints), orphans)
    return report


def _clear_prior_layer(writer: GraphWriter) -> None:
    """Drop this hook's own SERVES/ROUTES edges so a re-run rebuilds its layer (idempotent). Only
    `k8s-stitch`-provenanced edges are touched."""
    writer.query("MATCH ()-[r:SERVES]->() WHERE r.provenance = 'k8s-stitch' DELETE r")
    writer.query("MATCH ()-[r:ROUTES]->() WHERE r.provenance = 'k8s-stitch' DELETE r")
