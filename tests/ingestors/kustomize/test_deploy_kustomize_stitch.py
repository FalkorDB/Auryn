"""The `deploy-kustomize` stitch (ADR 0007): runs the shared image lifecycle over the overlay-emitted
hub images. Plus an end-to-end `ingest_directory` pass + whole-removal cleanup. Needs FalkorDB."""

from __future__ import annotations

from auryn.core import ingest
from auryn.core.graph_client import GraphWriter
from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.core.local_source import LocalDirectorySource
from auryn.ingestors.kustomize.git_sync import sync_at
from auryn.ingestors.kustomize.stitch import stitch

_KUST = (
    "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\n"
    "images:\n  - name: shop/api\n    newTag: \"1.0\"\n"
)


def _write(tmp_path):
    (tmp_path / "kustomization.yaml").write_text(_KUST)


def _ingest_kust(graph, tmp_path, project="shop"):
    _write(tmp_path)
    src = LocalDirectorySource(str(tmp_path))
    sync_at(GraphWriter(graph), src, src.tree_sha(), project_name=project, repo_uri="t", ref=None)


def _q(graph, c):
    return graph.query(c).result_set


def test_run_image_is_external_until_built(graph, tmp_path):
    writer = GraphWriter(graph)
    _ingest_kust(graph, tmp_path)
    writer.query("MATCH (i:Image {ref:'docker.io/shop/api:1.0'}) SET i.resolved = true, i.origin = 'internal'")
    stitch(writer)
    assert _q(graph, "MATCH (i:Image {ref:'docker.io/shop/api:1.0'}) RETURN i.resolved, i.origin") == [[False, "registry"]]


def test_run_image_flips_internal_when_built(graph, tmp_path):
    writer = GraphWriter(graph)
    _ingest_kust(graph, tmp_path)
    writer.merge_node(GraphNode("BuildStage", {"id": "docker://shop/file/Dockerfile#0"}, {"stage": "0"}))
    writer.merge_edge(GraphEdge("BUILDS_IMAGE", "BuildStage", {"id": "docker://shop/file/Dockerfile#0"},
                                "Image", {"id": "image://docker.io/shop/api:1.0"}, {"provenance": "compose-stitch"}))
    stitch(writer)
    assert _q(graph, "MATCH (i:Image {ref:'docker.io/shop/api:1.0'}) RETURN i.resolved, i.origin") == [[True, "internal"]]


def test_orphan_image_gc(graph, tmp_path):
    writer = GraphWriter(graph)
    _ingest_kust(graph, tmp_path)
    writer.merge_node(GraphNode("Image", {"id": "image://docker.io/library/orphan:1"}, {"ref": "docker.io/library/orphan:1"}))
    report = stitch(writer)
    assert report["orphans_removed"] == 1
    assert _q(graph, "MATCH (i:Image {ref:'docker.io/library/orphan:1'}) RETURN count(i)") == [[0]]


def test_end_to_end(graph, tmp_path):
    _write(tmp_path)
    ingest.ingest_directory(GraphWriter(graph), str(tmp_path), project_name="shop")
    assert _q(graph, "MATCH (:Resource)-[:RUNS_IMAGE]->(:Image {ref:'docker.io/shop/api:1.0'}) RETURN count(*)") == [[1]]
    assert _q(graph, "MATCH (i:Image {ref:'docker.io/shop/api:1.0'}) RETURN i.resolved, i.origin") == [[False, "registry"]]


def test_whole_removal_cleans_up_via_ingest(graph, tmp_path):
    # removing the kustomization leaves kustomize with 0 files -- but its prior `kustomize://` graph
    # triggers a cleanup resync (core.ingest) that recomputes to empty
    _write(tmp_path)
    ingest.ingest_directory(GraphWriter(graph), str(tmp_path), project_name="shop")
    assert _q(graph, "MATCH (n) WHERE n.id STARTS WITH 'kustomize://' RETURN count(n)")[0][0] >= 1
    (tmp_path / "kustomization.yaml").unlink()
    ingest.ingest_directory(GraphWriter(graph), str(tmp_path), project_name="shop")
    assert _q(graph, "MATCH (n) WHERE n.id STARTS WITH 'kustomize://' RETURN count(n)") == [[0]]
