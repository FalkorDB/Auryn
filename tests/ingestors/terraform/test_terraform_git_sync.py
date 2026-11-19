"""The Terraform producer against the real `graph` fixture: .tf -> Resource/Image/Database/Registry
nodes + DEPENDS_ON/RUNS_IMAGE/PROVISIONS edges, and resync. Needs FalkorDB (the conftest fixture)."""

from __future__ import annotations

from auryn.core.graph_client import GraphWriter
from auryn.core.local_source import LocalDirectorySource
from auryn.ingestors.terraform.git_sync import sync_at

_TF = (
    'resource "aws_ecr_repository" "api" { name = "shop/api" }\n'
    'resource "aws_db_instance" "db" { engine = "postgres" }\n'
    'resource "aws_ecs_task_definition" "api" {\n'
    '  container_definitions = jsonencode([{ name = "api", image = "shop/api:1.0" }])\n}\n'
    'resource "aws_ecs_service" "svc" { task_definition = aws_ecs_task_definition.api.arn }\n'
)


def _ingest(graph, tmp_path, files, project="shop"):
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    src = LocalDirectorySource(str(tmp_path))
    sync_at(GraphWriter(graph), src, src.tree_sha(), project_name=project, repo_uri="test://tf", ref=None)
    return lambda c: graph.query(c).result_set


def test_resources_and_containment(graph, tmp_path):
    q = _ingest(graph, tmp_path, {"main.tf": _TF})
    assert q("MATCH (r:Resource) RETURN count(r)") == [[4]]
    assert q("MATCH (:File)-[:HAS_RESOURCE]->(:Resource) RETURN count(*)") == [[4]]


def test_depends_on_from_reference(graph, tmp_path):
    q = _ingest(graph, tmp_path, {"main.tf": _TF})
    assert q("MATCH (:Resource {address:'aws_ecs_service.svc'})-[:DEPENDS_ON]->(r:Resource) RETURN r.address") \
        == [["aws_ecs_task_definition.api"]]


def test_runs_image_joins_hub(graph, tmp_path):
    q = _ingest(graph, tmp_path, {"main.tf": _TF})
    assert q("MATCH (:Resource {address:'aws_ecs_task_definition.api'})-[:RUNS_IMAGE]->(i:Image) RETURN i.ref") \
        == [["docker.io/shop/api:1.0"]]


def test_provisions_database_and_registry(graph, tmp_path):
    q = _ingest(graph, tmp_path, {"main.tf": _TF})
    assert q("MATCH (:Resource {rtype:'aws_db_instance'})-[:PROVISIONS]->(d:Database) RETURN d.name") == [["db"]]
    assert q("MATCH (:Resource {rtype:'aws_ecr_repository'})-[:PROVISIONS]->(r:Registry) RETURN count(r)") == [[1]]
    # the rds resource is classified
    assert q("MATCH (r:Resource {rtype:'aws_db_instance'}) RETURN r.category") == [["database"]]


def test_module_scoped_resources_stay_distinct(graph, tmp_path):
    # the SAME resource address in two module directories must be TWO nodes (env/dev vs env/prod)
    q = _ingest(graph, tmp_path, {
        "env/dev/main.tf": 'resource "aws_s3_bucket" "b" { bucket = "dev" }\n',
        "env/prod/main.tf": 'resource "aws_s3_bucket" "b" { bucket = "prod" }\n',
    })
    assert q("MATCH (r:Resource {address:'aws_s3_bucket.b'}) RETURN count(r)") == [[2]]
    assert sorted(r[0] for r in q("MATCH (r:Resource {address:'aws_s3_bucket.b'}) RETURN r.module")) == ["env/dev", "env/prod"]


def test_data_source_does_not_provision(graph, tmp_path):
    # a `data` lookup of an existing db is a READ, not a provision
    q = _ingest(graph, tmp_path, {"main.tf": 'data "aws_db_instance" "existing" { db_instance_identifier = "x" }\n'})
    assert q("MATCH (:Resource)-[:PROVISIONS]->() RETURN count(*)") == [[0]]
    assert q("MATCH (d:Database) RETURN count(d)") == [[0]]
    assert q("MATCH (r:Resource) RETURN r.category") == [["data"]]


def test_ordinary_resource_is_compute(graph, tmp_path):
    q = _ingest(graph, tmp_path, {"main.tf": 'resource "aws_ecs_task_definition" "api" { family = "x" }\n'})
    assert q("MATCH (r:Resource {rtype:'aws_ecs_task_definition'}) RETURN r.category") == [["compute"]]


def test_resync_removes_deleted_resource(graph, tmp_path):
    q = _ingest(graph, tmp_path, {"main.tf": _TF})
    assert q("MATCH (r:Resource) RETURN count(r)") == [[4]]
    q = _ingest(graph, tmp_path, {"main.tf": 'resource "aws_s3_bucket" "b" { bucket = "x" }\n'})
    assert sorted(r[0] for r in q("MATCH (r:Resource) RETURN r.address")) == ["aws_s3_bucket.b"]
    assert q("MATCH (:Resource)-[:DEPENDS_ON]->() RETURN count(*)") == [[0]]
    assert q("MATCH (d:Database) RETURN count(d)") == [[0]]
    assert q("MATCH (r:Registry) RETURN count(r)") == [[0]]  # the ecr Registry node was removed too


def test_resync_after_an_interrupted_sync_removes_the_last_resource(graph, tmp_path):
    _ingest(graph, tmp_path, {"main.tf": _TF})
    graph.query("MATCH (p:Project) SET p.commit_sha = NULL")  # what a sync that died mid-apply leaves
    (tmp_path / "main.tf").unlink()
    q = _ingest(graph, tmp_path, {})
    assert q("MATCH (r:Resource) RETURN count(r)") == [[0]]  # no stamp is not "never ingested"
