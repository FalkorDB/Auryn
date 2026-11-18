"""The `deploy` stitch -- the deployment tier's cross-artifact join (ADR 0007 §4-§6).

v1 implements the Docker arm: the **code seam** `PACKAGES`. A Dockerfile build stage copies host
files from its build context; this pass resolves those `COPY` paths to the actual code `File` nodes a
language ingestor produced and links them, so the golden path reads:

    code File <-PACKAGES- BuildStage -HAS_STAGE<- Dockerfile File

i.e. a query can go from a function/file to *which container image ships it* -- the code->container
connection, the mirror of the HTTP (ADR 0006) and ORM seams. It also keeps the shared `Image` hub
honest: `resolved` is recomputed (true iff something BUILDS_IMAGE the image) and orphan image nodes
(no builder, no runner, no base reference) are GC'd.

Graph-only + idempotent + order-independent: it owns its layer (the `deploy-stitch`-provenanced
edges + the image `resolved` flag), rebuilt from the current graph each run, so Dockerfile-first vs
code-first ingestion converge and a resync self-corrects. The other arms (`BUILDS_IMAGE` from a
Compose `build:`, `RUNS_IMAGE`/`SERVES`/`CONNECTS_TO`/`SHIPS`) land with the Compose/k8s/Terraform
producers -- their clears are already wired here so they compose into this one pass."""

from __future__ import annotations

import logging
import posixpath

from auryn.core.graph_client import GraphWriter
from auryn.core.graph_model import GraphEdge

logger = logging.getLogger("auryn.deploy")

_GLOB_CHARS = set("*?[")


def run(writer: GraphWriter, repo, target_sha: str) -> dict | None:
    """GraphEnrichment entrypoint. `repo`/`target_sha` are unused -- the stitch reads only the
    finished graph. Returns None when there's nothing to do (no build stages / no join)."""
    return stitch(writer)


def stitch(writer: GraphWriter) -> dict | None:
    try:
        has_stage = bool(writer.ro_query("MATCH (s:BuildStage) RETURN s.id LIMIT 1").result_set)
    except Exception:
        return None  # empty graph key ("operation on empty key") -> nothing to stitch

    _clear_prior_layer(writer)

    edges: list[GraphEdge] = []
    if has_stage:
        edges = _packages_edges(writer)
        if edges:
            writer.merge_edges(edges)

    _recompute_image_resolved(writer)
    orphans = _gc_orphan_images(writer)

    if not edges and not orphans:
        return None
    report = {"packages_edges": len(edges), "orphans_removed": orphans}
    logger.info("deploy stitch: %d PACKAGES edge(s), %d orphan image(s) GC'd", len(edges), orphans)
    return report


def _clear_prior_layer(writer: GraphWriter) -> None:
    """Drop this hook's own edges so a re-run rebuilds its layer from scratch (idempotent +
    order-independent). Only `deploy-stitch`-provenanced edges are touched; a producer's declared
    edges (BASED_ON/HAS_STAGE/...) are never this hook's and survive."""
    for rel in ("PACKAGES", "BUILDS_IMAGE", "SHIPS"):
        writer.query(f"MATCH ()-[r:{rel}]->() WHERE r.provenance = 'deploy-stitch' DELETE r")


# How each code ingestor exposes a source file's repo-relative path. Most languages emit a `File`
# node with `path`; Python models a source as a `Module` with `file_path` (no `File` node). The seam
# resolves code against every such convention so PACKAGES reaches whatever language built the graph --
# add a (label, path_property) pair here when a new ingestor uses a different code-source node.
_CODE_SOURCE_NODES = (("File", "path"), ("Module", "file_path"))


def _packages_edges(writer: GraphWriter) -> list[GraphEdge]:
    """For each BuildStage, resolve its host-context `COPY` sources to the code source nodes a
    language ingestor produced (File/Module, by path) and emit `BuildStage -PACKAGES-> <code node>`.
    Two reads per convention, matched in memory -- O(stages x copies x files), not a query per copy."""
    stages = writer.ro_query(
        "MATCH (s:BuildStage) RETURN s.id, s.context, s.copy_sources"
    ).result_set
    # Every code source node EXCEPT the Dockerfiles this tier emits (those are keyed `docker://`).
    code_nodes: list[tuple[str, str, str]] = []  # (id, path, label)
    for label, prop in _CODE_SOURCE_NODES:
        rows = writer.ro_query(
            f"MATCH (n:{label}) WHERE n.{prop} IS NOT NULL AND NOT n.id STARTS WITH 'docker://' "
            f"RETURN n.id, n.{prop}"
        ).result_set
        code_nodes.extend((str(nid), str(path), label) for nid, path in rows)
    if not code_nodes:
        return []

    edges: list[GraphEdge] = []
    for sid, context, copy_sources in stages:
        if not copy_sources:
            continue
        context = str(context or "")
        targets = [_target_path(context, str(src)) for src in copy_sources]
        matched: set[str] = set()
        for nid, path, label in code_nodes:
            if nid in matched:
                continue
            if any(_path_under(path, t) for t in targets):
                matched.add(nid)
                edges.append(GraphEdge("PACKAGES", "BuildStage", {"id": str(sid)}, label, {"id": nid},
                                       {"provenance": "deploy-stitch"}))
    return edges


def _target_path(context: str, src: str) -> str:
    """The repo-relative path a `COPY <src>` (relative to the build `context`) resolves to. `.` /`""`
    means the whole context (returned as the context dir, `""` = repo root). A trailing glob
    component is reduced to its containing directory (best-effort)."""
    src = src.strip()
    if src.startswith("./"):
        src = src[2:]
    if src in (".", "", "/"):
        return context
    src = src.rstrip("/")
    if any(ch in src for ch in _GLOB_CHARS):
        src = posixpath.dirname(src)
    joined = posixpath.join(context, src) if context else src
    return posixpath.normpath(joined) if joined else ""


def _path_under(path: str, target: str) -> bool:
    """A code file `path` is packaged by a copy resolving to `target` iff it IS that file or lives
    under that directory. `target == ""` is the whole repo root (a `COPY .` from a root context)."""
    if target == "":
        return True
    return path == target or path.startswith(target + "/")


def _recompute_image_resolved(writer: GraphWriter) -> None:
    """`resolved` = is this image built in-graph (some BUILDS_IMAGE points at it)? Recomputed over
    every Image so the flag is a pure function of the graph, independent of ingest order. `origin`
    follows: `internal` when built here, else `registry` (an external pull). No-op if no images."""
    writer.query(
        "MATCH (i:Image) OPTIONAL MATCH ()-[b:BUILDS_IMAGE]->(i) "
        "WITH i, count(b) AS builders "
        "SET i.resolved = builders > 0, i.origin = CASE WHEN builders > 0 THEN 'internal' ELSE 'registry' END"
    )


def _gc_orphan_images(writer: GraphWriter) -> int:
    """Delete `Image` nodes left with no inbound reference at all -- neither a base (`BASED_ON`), a
    builder (`BUILDS_IMAGE`), a runner (`RUNS_IMAGE`), nor a stage copy (`COPIES_FROM`). These are the
    residue of a removed Dockerfile/manifest; the `image://` node lives outside any project prefix, so
    a project resync can't reclaim it -- this does (the shared-node lifecycle, ADR 0007 §6)."""
    orphans = writer.ro_query(
        "MATCH (i:Image) WHERE NOT ()-[:BASED_ON]->(i) AND NOT ()-[:BUILDS_IMAGE]->(i) "
        "AND NOT ()-[:RUNS_IMAGE]->(i) AND NOT ()-[:COPIES_FROM]->(i) RETURN i.id"
    ).result_set
    if orphans:
        writer.query("MATCH (i:Image) WHERE i.id IN $ids DETACH DELETE i", {"ids": [r[0] for r in orphans]})
    return len(orphans)
