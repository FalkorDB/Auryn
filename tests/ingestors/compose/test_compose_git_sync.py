"""The Compose producer against the real `graph` fixture: compose files -> Service/Image nodes +
HAS_SERVICE/RUNS_IMAGE/DEPENDS_ON edges, and resync delta. Needs FalkorDB (the conftest fixture)."""

from __future__ import annotations

from auryn.core.graph_client import GraphWriter
from auryn.core.local_source import LocalDirectorySource
from auryn.ingestors.compose.git_sync import sync_at

_COMPOSE = (
    "services:\n"
    "  api:\n"
    "    build: ./api\n"
    "    image: shop/api:1.0\n"
    "    depends_on: [db, queue]\n"
    "  db:\n"
    "    image: postgres:16\n"
    "  queue:\n"
    "    image: redis:7\n"
)


def _ingest(graph, tmp_path, files, project="shop"):
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    src = LocalDirectorySource(str(tmp_path))
    sync_at(GraphWriter(graph), src, src.tree_sha(), project_name=project, repo_uri="test://compose", ref=None)
    return lambda cypher: graph.query(cypher).result_set


def test_services_and_containment(graph, tmp_path):
    q = _ingest(graph, tmp_path, {"docker-compose.yml": _COMPOSE})
    assert sorted(r[0] for r in q("MATCH (s:Service) RETURN s.name")) == ["api", "db", "queue"]
    assert q("MATCH (:Project)-[:HAS_FILE]->(:File)-[:HAS_SERVICE]->(s:Service) RETURN count(s)") == [[3]]


def test_runs_image_and_shared_hub(graph, tmp_path):
    q = _ingest(graph, tmp_path, {"docker-compose.yml": _COMPOSE})
    assert q("MATCH (:Service {name:'db'})-[:RUNS_IMAGE]->(i:Image) RETURN i.ref") == [["docker.io/library/postgres:16"]]
    # the published image is normalized through the shared helper
    assert q("MATCH (:Service {name:'api'})-[:RUNS_IMAGE]->(i:Image) RETURN i.ref") == [["docker.io/shop/api:1.0"]]


def test_depends_on_between_services(graph, tmp_path):
    q = _ingest(graph, tmp_path, {"docker-compose.yml": _COMPOSE})
    assert sorted(r[0] for r in q("MATCH (:Service {name:'api'})-[:DEPENDS_ON]->(d:Service) RETURN d.name")) == ["db", "queue"]


def test_build_dockerfile_recorded_for_stitch(graph, tmp_path):
    q = _ingest(graph, tmp_path, {"docker-compose.yml": _COMPOSE})
    # a building service records the repo-relative Dockerfile path the stitch resolves
    assert q("MATCH (s:Service {name:'api'}) RETURN s.build_dockerfile") == [["api/Dockerfile"]]
    assert q("MATCH (s:Service {name:'db'}) RETURN s.build_dockerfile") == [[None]]


def test_resync_removes_deleted_service(graph, tmp_path):
    q = _ingest(graph, tmp_path, {"docker-compose.yml": _COMPOSE})
    assert q("MATCH (s:Service) RETURN count(s)") == [[3]]
    q = _ingest(graph, tmp_path, {"docker-compose.yml": "services:\n  api:\n    image: shop/api:2.0\n"})
    assert sorted(r[0] for r in q("MATCH (s:Service) RETURN s.name")) == ["api"]
    assert q("MATCH (:Service)-[:DEPENDS_ON]->() RETURN count(*)") == [[0]]
