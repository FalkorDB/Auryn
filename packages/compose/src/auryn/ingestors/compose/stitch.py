"""The `deploy-compose` stitch -- closes the build->run loop (ADR 0007 §4).

A compose service that both `build:`s a Dockerfile and runs an `image:` tells us that Dockerfile
PRODUCES that image. This pass resolves the service's Dockerfile path to the right `BuildStage` (the
docker producer's nodes, matched by path -- no package dependency) and links them:

    api#final (BuildStage) -BUILDS_IMAGE-> shop/api:1.0 (Image) <-RUNS_IMAGE- Service api

Now the image has a builder, so it flips `resolved:true`/`origin:internal` -- the hub becomes a real
join between the code a Dockerfile packages and the service that runs it.

Graph-only + idempotent + order-independent: it owns its layer (the `compose-stitch` BUILDS_IMAGE
edges), rebuilt each run, then recomputes image `resolved` and GCs orphan images via the shared
`core.deploy` helpers (so cleanup happens even when docker's `deploy` hook is disabled). Scoped
per-project so a service only matches a Dockerfile in its OWN repo."""

from __future__ import annotations

import logging

from auryn.core import deploy
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


def _is_empty_graph(exc: Exception) -> bool:
    """A query over a graph key that doesn't exist yet raises an 'operation on empty key' error -- the
    one condition we swallow (nothing to stitch). Any other error propagates so the runner records it."""
    return "empty key" in str(exc).lower()


def stitch(writer: GraphWriter) -> dict | None:
    try:
        services = writer.ro_query(
            "MATCH (s:Service)-[:RUNS_IMAGE]->(img:Image) WHERE coalesce(s.build_dockerfile, '') <> '' "
            "RETURN s.id, s.build_dockerfile, s.build_target, img.id"
        ).result_set
    except Exception as e:
        if _is_empty_graph(e):
            return None
        raise

    _clear_prior_layer(writer)
    edges = _builds_image_edges(writer, services)
    if edges:
        writer.merge_edges(edges)
    deploy.recompute_image_resolved(writer)
    orphans = deploy.gc_orphan_images(writer)

    if not edges and not orphans:
        return None
    report = {"builds_image_edges": len(edges), "orphans_removed": orphans}
    logger.info("deploy-compose: %d BUILDS_IMAGE edge(s), %d orphan image(s) GC'd", len(edges), orphans)
    return report


def _clear_prior_layer(writer: GraphWriter) -> None:
    """Drop this hook's own BUILDS_IMAGE edges so a re-run rebuilds its layer (idempotent). Only
    `compose-stitch`-provenanced edges are touched -- never a declared edge or another layer's."""
    writer.query("MATCH ()-[r:BUILDS_IMAGE]->() WHERE r.provenance = 'compose-stitch' DELETE r")


def _builds_image_edges(writer: GraphWriter, services) -> list[GraphEdge]:
    """For each building service, resolve the BuildStage its image comes from and link it. The stage is
    the one named by `build.target` when set, else the Dockerfile's FINAL stage (highest index) -- a
    `target: test` service must build the `test` stage, not production, or blast-radius runs through the
    wrong code."""
    edges: list[GraphEdge] = []
    for sid, dockerfile, target, img_id in services:
        proj = _project_of(str(sid))
        stage_id = _resolve_stage(writer, proj, str(dockerfile), str(target or ""))
        if stage_id:
            edges.append(GraphEdge("BUILDS_IMAGE", "BuildStage", {"id": stage_id},
                                   "Image", {"id": str(img_id)}, {"provenance": "compose-stitch"}))
    return edges


def _resolve_stage(writer: GraphWriter, proj: str, dockerfile: str, target: str) -> str | None:
    """The BuildStage id a service's image comes from, scoped to its own project. With `target`, match
    that stage BY NAME (no fallback -- a missing target must NOT mislink to the final stage); otherwise
    the final stage (highest index)."""
    if target:
        rows = writer.ro_query(
            "MATCH (f:File)-[:HAS_STAGE]->(st:BuildStage) "
            "WHERE f.path = $p AND f.id STARTS WITH $pfx AND st.stage = $t RETURN st.id LIMIT 1",
            {"p": dockerfile, "pfx": f"docker://{proj}/", "t": target},
        ).result_set
    else:
        rows = writer.ro_query(
            "MATCH (f:File)-[:HAS_STAGE]->(st:BuildStage) WHERE f.path = $p AND f.id STARTS WITH $pfx "
            "RETURN st.id ORDER BY st.index DESC LIMIT 1",
            {"p": dockerfile, "pfx": f"docker://{proj}/"},
        ).result_set
    return str(rows[0][0]) if rows else None
