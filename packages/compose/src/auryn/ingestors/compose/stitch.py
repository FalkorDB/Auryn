"""The `deploy-compose` stitch -- closes the build->run loop (ADR 0007 §4).

A compose service that both `build:`s a Dockerfile and runs an `image:` tells us that Dockerfile
PRODUCES that image. This pass resolves the service's Dockerfile path to the Dockerfile's final
`BuildStage` (the docker producer's nodes, matched by path -- no package dependency) and links them:

    api#final (BuildStage) -BUILDS_IMAGE-> shop/api:1.0 (Image) <-RUNS_IMAGE- Service api

Now the image has a builder, so it flips `resolved:true`/`origin:internal` -- the hub becomes a real
join between the code a Dockerfile packages and the service that runs it. The golden path spans the
whole stack: a function -> its module -> the BuildStage that packages it -> the image it builds ->
the running service -> everything that service depends on.

Graph-only + idempotent + order-independent: it owns its layer (the `compose-stitch` BUILDS_IMAGE
edges), rebuilt each run, and recomputes image `resolved` as a pure function of the graph. Scoped
per-project so a service only matches a Dockerfile in its OWN repo."""

from __future__ import annotations

import logging

from auryn.core.graph_client import GraphWriter
from auryn.core.graph_model import GraphEdge

logger = logging.getLogger("auryn.deploy-compose")


def run(writer: GraphWriter, repo, target_sha: str) -> dict | None:
    """GraphEnrichment entrypoint. Reads only the finished graph."""
    return stitch(writer)


def _project_of(node_id: str) -> str:
    if "://" not in node_id:
        return ""
    return node_id.split("://", 1)[1].split("/", 1)[0]


def stitch(writer: GraphWriter) -> dict | None:
    try:
        services = writer.ro_query(
            "MATCH (s:Service)-[:RUNS_IMAGE]->(img:Image) WHERE s.build_dockerfile IS NOT NULL "
            "RETURN s.id, s.build_dockerfile, img.id"
        ).result_set
    except Exception:
        return None  # empty graph key -> nothing to stitch

    _clear_prior_layer(writer)
    edges = _builds_image_edges(writer, services)
    if edges:
        writer.merge_edges(edges)
    _recompute_image_resolved(writer)

    if not edges:
        return None
    report = {"builds_image_edges": len(edges)}
    logger.info("deploy-compose: %d BUILDS_IMAGE edge(s)", len(edges))
    return report


def _clear_prior_layer(writer: GraphWriter) -> None:
    """Drop this hook's own BUILDS_IMAGE edges so a re-run rebuilds its layer (idempotent). Only
    `compose-stitch`-provenanced edges are touched -- never a declared edge or another layer's."""
    writer.query("MATCH ()-[r:BUILDS_IMAGE]->() WHERE r.provenance = 'compose-stitch' DELETE r")


def _builds_image_edges(writer: GraphWriter, services) -> list[GraphEdge]:
    """For each building service, find the FINAL BuildStage of its Dockerfile (same project, highest
    stage index -- the stage that produces the published image) and link it to the service's image."""
    edges: list[GraphEdge] = []
    for sid, dockerfile, img_id in services:
        proj = _project_of(str(sid))
        rows = writer.ro_query(
            "MATCH (f:File)-[:HAS_STAGE]->(st:BuildStage) "
            "WHERE f.path = $p AND f.id STARTS WITH $pfx "
            "RETURN st.id ORDER BY st.index DESC LIMIT 1",
            {"p": str(dockerfile), "pfx": f"docker://{proj}/"},
        ).result_set
        if rows:
            edges.append(GraphEdge("BUILDS_IMAGE", "BuildStage", {"id": str(rows[0][0])},
                                   "Image", {"id": str(img_id)}, {"provenance": "compose-stitch"}))
    return edges


def _recompute_image_resolved(writer: GraphWriter) -> None:
    """`resolved` = is this image built in-graph (some BUILDS_IMAGE points at it)? Pure function of the
    graph, so this is correct regardless of which deploy arm (docker/compose) ran last."""
    writer.query(
        "MATCH (i:Image) OPTIONAL MATCH ()-[b:BUILDS_IMAGE]->(i) "
        "WITH i, count(b) AS builders "
        "SET i.resolved = builders > 0, i.origin = CASE WHEN builders > 0 THEN 'internal' ELSE 'registry' END"
    )
