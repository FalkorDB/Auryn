"""Shared image-lifecycle operations for the deployment tier (ADR 0007), so every deploy arm
(docker `deploy`, compose `deploy-compose`, future k8s/terraform) applies the SAME semantics and they
can't diverge. Each arm calls these after writing its own edges."""

from __future__ import annotations

from auryn.core.graph_client import GraphWriter


def recompute_image_resolved(writer: GraphWriter) -> None:
    """`resolved` = is this image built in-graph (some `BUILDS_IMAGE` points at it)? Recomputed over
    every `Image` so the flag is a pure function of the graph -- correct regardless of which deploy arm
    ran last, and of ingest order. `origin` follows: `internal` when built here, else `registry`."""
    writer.query(
        "MATCH (i:Image) OPTIONAL MATCH ()-[b:BUILDS_IMAGE]->(i) "
        "WITH i, count(b) AS builders "
        "SET i.resolved = builders > 0, i.origin = CASE WHEN builders > 0 THEN 'internal' ELSE 'registry' END"
    )


def gc_orphan_images(writer: GraphWriter) -> int:
    """Delete `Image` nodes left with NO inbound reference -- no base (`BASED_ON`), builder
    (`BUILDS_IMAGE`), runner (`RUNS_IMAGE`), nor provisioner (`PROVISIONS`). These are the residue of a
    removed Dockerfile/compose service/manifest; the shared `image://` node lives outside any project
    prefix, so a project resync can't reclaim it. Predicate test AND delete in ONE write query so a
    concurrent ingest can't reference an image between a separate check and delete (TOCTOU). Any deploy
    arm can call it, so cleanup still happens even if another arm's hook is disabled."""
    res = writer.query(
        "MATCH (i:Image) WHERE NOT ()-[:BASED_ON]->(i) AND NOT ()-[:BUILDS_IMAGE]->(i) "
        "AND NOT ()-[:RUNS_IMAGE]->(i) AND NOT ()-[:PROVISIONS]->(i) "
        "WITH i, i.id AS gone DETACH DELETE i RETURN count(gone)"
    )
    rows = res.result_set
    return int(rows[0][0]) if rows and rows[0][0] else 0
