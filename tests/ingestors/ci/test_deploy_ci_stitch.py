"""The `deploy-ci` stitch (ADR 0007): a workflow's `BUILDS_IMAGE` flips a hub image the rest of the
tier only RUNS to resolved/internal -- closing the build->image->run loop. Plus GC + end-to-end."""

from __future__ import annotations

from auryn.core import ingest
from auryn.core.graph_client import GraphWriter
from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.core.local_source import LocalDirectorySource
from auryn.ingestors.ci.git_sync import sync_at
from auryn.ingestors.ci.stitch import stitch

_WF = (
    "name: build\non: [push]\njobs:\n  build:\n    steps:\n"
    "      - uses: docker/build-push-action@v5\n        with:\n          push: true\n          tags: shop/api:1.0\n"
)


def _write(tmp_path):
    p = tmp_path / ".github/workflows/build.yml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_WF)


def _ingest_wf(graph, tmp_path, project="shop"):
    _write(tmp_path)
    src = LocalDirectorySource(str(tmp_path))
    sync_at(GraphWriter(graph), src, src.tree_sha(), project_name=project, repo_uri="t", ref=None)


def _q(graph, c):
    return graph.query(c).result_set


def test_ci_build_flips_a_run_image_internal(graph, tmp_path):
    # THE headline: CI BUILDS the image another producer only RUNS -> the stitch flips it internal
    writer = GraphWriter(graph)
    _ingest_wf(graph, tmp_path)  # workflow BUILDS_IMAGE docker.io/shop/api:1.0
    writer.merge_node(GraphNode("Service", {"id": "compose://shop/file/compose.yml/api"}, {"name": "api"}))
    writer.merge_edge(GraphEdge("RUNS_IMAGE", "Service", {"id": "compose://shop/file/compose.yml/api"},
                                "Image", {"id": "image://docker.io/shop/api:1.0"}, {}))
    writer.query("MATCH (i:Image {ref:'docker.io/shop/api:1.0'}) SET i.resolved = false, i.origin = 'registry'")
    stitch(writer)
    assert _q(graph, "MATCH (i:Image {ref:'docker.io/shop/api:1.0'}) RETURN i.resolved, i.origin") == [[True, "internal"]]


def test_orphan_image_gc(graph, tmp_path):
    writer = GraphWriter(graph)
    _ingest_wf(graph, tmp_path)
    writer.merge_node(GraphNode("Image", {"id": "image://docker.io/library/orphan:1"}, {"ref": "docker.io/library/orphan:1"}))
    report = stitch(writer)
    assert report["orphans_removed"] == 1
    assert _q(graph, "MATCH (i:Image {ref:'docker.io/library/orphan:1'}) RETURN count(i)") == [[0]]


def test_end_to_end(graph, tmp_path):
    _write(tmp_path)
    ingest.ingest_directory(GraphWriter(graph), str(tmp_path), project_name="shop")
    # the producer ran (the workflow BUILDS the image) and the stitch marked it internal (built in-graph)
    assert _q(graph, "MATCH (:Resource)-[:BUILDS_IMAGE]->(:Image {ref:'docker.io/shop/api:1.0'}) RETURN count(*)") == [[1]]
    assert _q(graph, "MATCH (i:Image {ref:'docker.io/shop/api:1.0'}) RETURN i.resolved, i.origin") == [[True, "internal"]]


def test_whole_removal_cleans_up_via_ingest(graph, tmp_path):
    _write(tmp_path)
    ingest.ingest_directory(GraphWriter(graph), str(tmp_path), project_name="shop")
    assert _q(graph, "MATCH (n) WHERE n.id STARTS WITH 'ci://' RETURN count(n)")[0][0] >= 1
    (tmp_path / ".github/workflows/build.yml").unlink()
    ingest.ingest_directory(GraphWriter(graph), str(tmp_path), project_name="shop")
    assert _q(graph, "MATCH (n) WHERE n.id STARTS WITH 'ci://' RETURN count(n)") == [[0]]
