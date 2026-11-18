"""The k8s producer against the real `graph` fixture: manifests -> Service (workload)/Image/Ingress
nodes + RUNS_IMAGE/HAS_SERVICE/HAS_INGRESS edges, and resync. Needs FalkorDB (the conftest fixture)."""

from __future__ import annotations

from auryn.core.graph_client import GraphWriter
from auryn.core.local_source import LocalDirectorySource
from auryn.ingestors.k8s.git_sync import sync_at

_MANIFEST = (
    "apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: api, namespace: shop}\n"
    "spec:\n  template:\n    spec:\n      containers:\n        - {name: api, image: shop/api:1.0}\n"
    "---\n"
    "apiVersion: networking.k8s.io/v1\nkind: Ingress\nmetadata: {name: ing, namespace: shop}\n"
    "spec:\n  rules:\n    - host: shop.example.com\n      http:\n        paths:\n"
    "          - path: /api\n            backend: {service: {name: api}}\n"
)


def _ingest(graph, tmp_path, files, project="shop"):
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    src = LocalDirectorySource(str(tmp_path))
    sync_at(GraphWriter(graph), src, src.tree_sha(), project_name=project, repo_uri="test://k8s", ref=None)
    return lambda c: graph.query(c).result_set


def test_workload_runs_image(graph, tmp_path):
    q = _ingest(graph, tmp_path, {"k8s/deploy.yaml": _MANIFEST})
    assert q("MATCH (s:Service {name:'api'}) RETURN s.kind, s.namespace") == [["Deployment", "shop"]]
    assert q("MATCH (:Service {name:'api'})-[:RUNS_IMAGE]->(i:Image) RETURN i.ref") == [["docker.io/shop/api:1.0"]]
    assert q("MATCH (:File)-[:HAS_SERVICE]->(:Service) RETURN count(*)") == [[1]]


def test_ingress_node(graph, tmp_path):
    q = _ingest(graph, tmp_path, {"k8s/deploy.yaml": _MANIFEST})
    assert q("MATCH (i:Ingress) RETURN i.route, i.backend_service, i.namespace") == [["/api", "api", "shop"]]
    assert q("MATCH (:File)-[:HAS_INGRESS]->(:Ingress) RETURN count(*)") == [[1]]


def test_non_manifest_yaml_is_ignored(graph, tmp_path):
    # a compose file (no apiVersion/kind) sits in the tree but must NOT create k8s nodes
    q = _ingest(graph, tmp_path, {
        "k8s/deploy.yaml": _MANIFEST,
        "docker-compose.yml": "services:\n  web:\n    image: nginx\n",
        "config.yaml": "name: not-k8s\nvalue: 1\n",
    })
    # only the Deployment is a k8s Service; the compose/config yaml contribute nothing here
    assert q("MATCH (s:Service) RETURN count(s)") == [[1]]
    assert sorted(r[0] for r in q("MATCH (f:File) RETURN f.path")) == ["k8s/deploy.yaml"]


def test_resync_removes_deleted_workload(graph, tmp_path):
    q = _ingest(graph, tmp_path, {"k8s/deploy.yaml": _MANIFEST})
    assert q("MATCH (s:Service) RETURN count(s)") == [[1]]
    q = _ingest(graph, tmp_path, {"k8s/deploy.yaml":
        "apiVersion: v1\nkind: Pod\nmetadata: {name: p, namespace: shop}\nspec:\n  containers:\n    - {name: c, image: x}\n"})
    assert sorted(r[0] for r in q("MATCH (s:Service) RETURN s.name")) == ["p"]
    assert q("MATCH (i:Ingress) RETURN count(i)") == [[0]]  # the Ingress was removed
