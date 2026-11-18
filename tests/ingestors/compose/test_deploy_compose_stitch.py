"""The `deploy-compose` stitch (ADR 0007): closes the build->run loop -- a service's `build:` resolves
to the Dockerfile's final BuildStage, emits BUILDS_IMAGE, and flips the image to resolved/internal.
Plus an end-to-end pass through the real ingest endpoint. Needs FalkorDB (the conftest fixture)."""

from __future__ import annotations

from auryn.core import ingest
from auryn.core.graph_client import GraphWriter
from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.core.local_source import LocalDirectorySource
from auryn.ingestors.compose.git_sync import sync_at
from auryn.ingestors.compose.stitch import stitch

_COMPOSE = "services:\n  api:\n    build: ./api\n    image: shop/api:1.0\n  db:\n    image: postgres:16\n"


def _seed_dockerfile_stages(writer, project="shop"):
    """Simulate the docker producer: a 2-stage api Dockerfile (builder #0, final #1)."""
    fid = f"docker://{project}/file/api/Dockerfile"
    writer.merge_node(GraphNode("File", {"id": fid}, {"path": "api/Dockerfile"}))
    for name, idx in (("builder", 0), ("final", 1)):
        sid = f"{fid}#{name}"
        writer.merge_node(GraphNode("BuildStage", {"id": sid}, {"stage": name, "index": idx}))
        writer.merge_edge(GraphEdge("HAS_STAGE", "File", {"id": fid}, "BuildStage", {"id": sid}))


def _ingest_compose(graph, tmp_path, project="shop"):
    (tmp_path / "docker-compose.yml").write_text(_COMPOSE)
    src = LocalDirectorySource(str(tmp_path))
    sync_at(GraphWriter(graph), src, src.tree_sha(), project_name=project, repo_uri="test://compose", ref=None)


def _q(graph, cypher):
    return graph.query(cypher).result_set


def test_builds_image_links_final_stage(graph, tmp_path):
    writer = GraphWriter(graph)
    _seed_dockerfile_stages(writer)
    _ingest_compose(graph, tmp_path)

    report = stitch(writer)
    assert report is not None and report["builds_image_edges"] == 1
    # the FINAL stage (highest index), not the builder, builds the published image
    assert _q(graph, "MATCH (st:BuildStage)-[:BUILDS_IMAGE]->(:Image {ref:'docker.io/shop/api:1.0'}) RETURN st.stage") == [["final"]]


def test_image_flips_resolved_internal(graph, tmp_path):
    writer = GraphWriter(graph)
    _seed_dockerfile_stages(writer)
    _ingest_compose(graph, tmp_path)
    stitch(writer)
    # the built image is now internal; the external pull (postgres) stays external
    assert _q(graph, "MATCH (i:Image {ref:'docker.io/shop/api:1.0'}) RETURN i.resolved, i.origin") == [[True, "internal"]]
    assert _q(graph, "MATCH (i:Image {ref:'docker.io/library/postgres:16'}) RETURN i.resolved, i.origin") == [[False, "registry"]]


def test_no_builder_without_a_matching_dockerfile(graph, tmp_path):
    # no docker BuildStage seeded -> nothing to link; the image stays external
    writer = GraphWriter(graph)
    _ingest_compose(graph, tmp_path)
    assert stitch(writer) is None
    assert _q(graph, "MATCH ()-[r:BUILDS_IMAGE]->() RETURN count(r)") == [[0]]
    assert _q(graph, "MATCH (i:Image {ref:'docker.io/shop/api:1.0'}) RETURN i.resolved") == [[False]]


def test_stitch_is_idempotent(graph, tmp_path):
    writer = GraphWriter(graph)
    _seed_dockerfile_stages(writer)
    _ingest_compose(graph, tmp_path)
    stitch(writer)
    first = _q(graph, "MATCH ()-[r:BUILDS_IMAGE]->() RETURN count(r)")[0][0]
    stitch(writer)
    assert _q(graph, "MATCH ()-[r:BUILDS_IMAGE]->() RETURN count(r)")[0][0] == first == 1


def test_builds_image_scoped_per_project(graph, tmp_path):
    # a Dockerfile with the same path in ANOTHER project must NOT satisfy this service's build
    writer = GraphWriter(graph)
    _seed_dockerfile_stages(writer, project="other")   # same api/Dockerfile path, different repo
    _ingest_compose(graph, tmp_path, project="shop")
    assert stitch(writer) is None  # shop's service finds no shop Dockerfile
    assert _q(graph, "MATCH ()-[r:BUILDS_IMAGE]->() RETURN count(r)") == [[0]]


def test_end_to_end_through_ingest_endpoint(graph, tmp_path):
    """Full pipeline: docker + compose producers + both deploy arms. A function traces to the running
    service that ships it, and the built image is internal."""
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "main.py").write_text("def handler():\n    return 'ok'\n")
    (tmp_path / "api" / "Dockerfile").write_text("FROM python:3.12-slim\nWORKDIR /app\nCOPY . /app\n")
    (tmp_path / "docker-compose.yml").write_text(_COMPOSE)

    report = ingest.ingest_directory(GraphWriter(graph), str(tmp_path), project_name="shop")
    assert "deploy-compose" in report.enrichments

    # the full-stack path: function -> module -> stage -> built image <- running service
    row = _q(graph,
        "MATCH (:Function {name:'handler'})<-[:HAS_FUNCTION]-(:Module)<-[:PACKAGES]-(:BuildStage)"
        "-[:BUILDS_IMAGE]->(i:Image)<-[:RUNS_IMAGE]-(s:Service) RETURN i.ref, i.resolved, s.name")
    assert row == [["docker.io/shop/api:1.0", True, "api"]]
