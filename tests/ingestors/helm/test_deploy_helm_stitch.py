"""The `deploy-helm` stitch (ADR 0007): runs the shared image lifecycle over the chart-emitted hub
images. Plus an end-to-end pass. Needs FalkorDB (the conftest fixture)."""

from __future__ import annotations

from auryn.core import ingest
from auryn.core.graph_client import GraphWriter
from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.core.local_source import LocalDirectorySource
from auryn.ingestors.helm.git_sync import sync_at
from auryn.ingestors.helm.stitch import stitch

_CHART = "apiVersion: v2\nname: shop-api\nversion: 1.0.0\n"
_VALUES = "image:\n  repository: shop/api\n  tag: \"1.0\"\n"
_DEPLOYMENT = (
    "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: shop-api\nspec:\n  template:\n    spec:\n"
    '      containers:\n        - image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"\n'
)


def _ingest_chart(graph, tmp_path, project="shop"):
    for rel, text in {
        "charts/shop/Chart.yaml": _CHART,
        "charts/shop/values.yaml": _VALUES,
        "charts/shop/templates/deployment.yaml": _DEPLOYMENT,
    }.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    src = LocalDirectorySource(str(tmp_path))
    sync_at(GraphWriter(graph), src, src.tree_sha(), project_name=project, repo_uri="t", ref=None)


def _q(graph, c):
    return graph.query(c).result_set


def test_helm_run_image_is_external_until_built(graph, tmp_path):
    writer = GraphWriter(graph)
    _ingest_chart(graph, tmp_path)
    # force a stale resolved:true, so the assertion verifies the STITCH recomputed it
    writer.query("MATCH (i:Image {ref:'docker.io/shop/api:1.0'}) SET i.resolved = true, i.origin = 'internal'")
    stitch(writer)
    assert _q(graph, "MATCH (i:Image {ref:'docker.io/shop/api:1.0'}) RETURN i.resolved, i.origin") == [[False, "registry"]]


def test_helm_run_image_flips_internal_when_built(graph, tmp_path):
    writer = GraphWriter(graph)
    _ingest_chart(graph, tmp_path)
    # simulate docker/compose having built it (a BUILDS_IMAGE edge) -> the stitch's recompute flips it
    writer.merge_node(GraphNode("BuildStage", {"id": "docker://shop/file/Dockerfile#0"}, {"stage": "0"}))
    writer.merge_edge(GraphEdge("BUILDS_IMAGE", "BuildStage", {"id": "docker://shop/file/Dockerfile#0"},
                                "Image", {"id": "image://docker.io/shop/api:1.0"}, {"provenance": "compose-stitch"}))
    stitch(writer)
    assert _q(graph, "MATCH (i:Image {ref:'docker.io/shop/api:1.0'}) RETURN i.resolved, i.origin") == [[True, "internal"]]


def test_orphan_image_gc(graph, tmp_path):
    writer = GraphWriter(graph)
    _ingest_chart(graph, tmp_path)
    writer.merge_node(GraphNode("Image", {"id": "image://docker.io/library/orphan:1"}, {"ref": "docker.io/library/orphan:1"}))
    report = stitch(writer)
    assert report["orphans_removed"] == 1
    assert _q(graph, "MATCH (i:Image {ref:'docker.io/library/orphan:1'}) RETURN count(i)") == [[0]]


def test_end_to_end(graph, tmp_path):
    for rel, text in {
        "charts/shop/Chart.yaml": _CHART,
        "charts/shop/values.yaml": _VALUES,
        "charts/shop/templates/deployment.yaml": _DEPLOYMENT,
    }.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    ingest.ingest_directory(GraphWriter(graph), str(tmp_path), project_name="shop")
    # the producer ran (the chart workload RUNS the hub image) and the stitch recomputed its state
    assert _q(graph, "MATCH (:Service)-[:RUNS_IMAGE]->(:Image {ref:'docker.io/shop/api:1.0'}) RETURN count(*)") == [[1]]
    assert _q(graph, "MATCH (i:Image {ref:'docker.io/shop/api:1.0'}) RETURN i.resolved, i.origin") == [[False, "registry"]]
