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
import re

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


def _project_of(node_id: str) -> str:
    """The `<project>` component of a graph id (`<scheme>://<project>/...`), or "" if unparseable.
    A Dockerfile and the code it ships, ingested from one repo, share it (`docker://demo`,
    `python://demo`) -- so it scopes the PACKAGES join to the stage's OWN repo and never links an
    image to another co-located repo's files that merely share a relative path."""
    if "://" not in node_id:
        return ""
    return node_id.split("://", 1)[1].split("/", 1)[0]


def _packages_edges(writer: GraphWriter) -> list[GraphEdge]:
    """For each BuildStage, resolve its host-context `COPY` sources to the code source nodes a
    language ingestor produced (File/Module, by path) and emit `BuildStage -PACKAGES-> <code node>`.
    Matching is scoped to the stage's own project (a shared graph holds many repos). Two reads per
    convention, matched in memory -- O(stages x copies x files), not a query per copy."""
    stages = writer.ro_query(
        "MATCH (s:BuildStage) RETURN s.id, s.context, s.copy_sources"
    ).result_set
    # Every code source node EXCEPT the Dockerfiles this tier emits (those are keyed `docker://`).
    code_nodes: list[tuple[str, str, str, str]] = []  # (id, path, label, project)
    for label, prop in _CODE_SOURCE_NODES:
        rows = writer.ro_query(
            f"MATCH (n:{label}) WHERE n.{prop} IS NOT NULL AND NOT n.id STARTS WITH 'docker://' "
            f"RETURN n.id, n.{prop}"
        ).result_set
        code_nodes.extend((str(nid), str(path), label, _project_of(str(nid))) for nid, path in rows)
    if not code_nodes:
        return []

    edges: list[GraphEdge] = []
    for sid, context, copy_sources in stages:
        if not copy_sources:
            continue
        stage_proj = _project_of(str(sid))  # PACKAGES only reaches THIS Dockerfile's own repo
        context = str(context or "")
        targets = [_target_path(context, str(src)) for src in copy_sources]
        matched: set[str] = set()
        for nid, path, label, proj in code_nodes:
            if nid in matched or proj != stage_proj:
                continue
            if any(_path_under(path, t) for t in targets):
                matched.add(nid)
                edges.append(GraphEdge("PACKAGES", "BuildStage", {"id": str(sid)}, label, {"id": nid},
                                       {"provenance": "deploy-stitch"}))
    return edges


def _target_path(context: str, src: str) -> str:
    """The repo-relative path (or glob PATTERN) a `COPY <src>` resolves to, relative to the build
    `context`. `.`/`""` means the whole context (returned as the context dir, `""` = repo root). A
    glob is kept VERBATIM so `_path_under` matches the pattern per segment, not every sibling."""
    src = src.strip()
    if src.startswith("./"):
        src = src[2:]
    if src in (".", "", "/"):
        return context
    # Docker resolves COPY sources from the build-context ROOT: a leading `/` is dropped (not an
    # absolute host path, so `COPY /app` means `<context>/app`), and `..` that would escape the context
    # is removed (BuildKit forbids it) -- otherwise `COPY /../app` would normalize ABOVE the context and
    # mis-link a sibling repo's file.
    src = src.lstrip("/").rstrip("/")
    while src.startswith("../"):
        src = src[3:]
    if src in ("..", ""):
        return context
    joined = posixpath.join(context, src) if context else src
    norm = posixpath.normpath(joined) if joined else ""
    return "" if norm == "." else norm


def _glob_to_regex(pattern: str) -> re.Pattern:
    """Translate a Docker COPY glob to a regex with Go `filepath.Match` semantics: `*` and `?` do NOT
    cross `/` (unlike `fnmatch`), so `src/*.py` matches `src/app.py` but not `src/sub/app.py`. `[...]`
    character classes pass through; every other char is literal."""
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = pattern.find("]", i + 1)
            if j == -1:
                out.append(re.escape(c))
            else:
                out.append(pattern[i : j + 1])  # a literal char class, valid regex as-is
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def _path_under(path: str, target: str) -> bool:
    """A code file `path` is packaged by a copy resolving to `target` iff: the target is the whole
    context root (`""`); the target is a glob and the path matches it per segment (`COPY src/*.py`
    links `src/app.py`, not `src/README` nor nested `src/sub/app.py`); or the path IS that file or
    lives under that directory."""
    if target == "":
        return True
    if any(ch in target for ch in _GLOB_CHARS):
        return _glob_to_regex(target).match(path) is not None
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
    """Delete `Image` nodes left with no inbound reference at all -- no base (`BASED_ON`), builder
    (`BUILDS_IMAGE`), runner (`RUNS_IMAGE`), nor provisioner (`PROVISIONS`, a Terraform `Resource`).
    (`COPIES_FROM` is `BuildStage -> BuildStage` and can never target an `Image`, so it's not a keep
    reason.) These are the residue of a removed Dockerfile/manifest; the `image://` node lives outside
    any project prefix, so a project resync can't reclaim it -- this does (shared-node lifecycle, §6)."""
    # Predicate test AND delete in ONE write query: a separate check-then-delete could DETACH DELETE
    # an image a concurrent ingest referenced in between (the delete rechecked only ids, not the
    # orphan predicate). One query closes that TOCTOU -- same pattern as the http stitch's orphan GC.
    res = writer.query(
        "MATCH (i:Image) WHERE NOT ()-[:BASED_ON]->(i) AND NOT ()-[:BUILDS_IMAGE]->(i) "
        "AND NOT ()-[:RUNS_IMAGE]->(i) AND NOT ()-[:PROVISIONS]->(i) "
        "WITH i, i.id AS gone DETACH DELETE i RETURN count(gone)"
    )
    rows = res.result_set
    return int(rows[0][0]) if rows and rows[0][0] else 0
