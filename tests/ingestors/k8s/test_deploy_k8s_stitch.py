"""The `deploy-k8s` stitch (ADR 0007 + ADR 0006): an Ingress route keys the shared route-keyed
APIEndpoint and the backing workload SERVES it. Plus an end-to-end pass proving the endpoint survives
the http-endpoints GC. Needs FalkorDB (the conftest fixture)."""

from __future__ import annotations

from auryn.core import ingest
from auryn.core.graph_client import GraphWriter
from auryn.core.http_seam import endpoint_placeholder_key
from auryn.core.local_source import LocalDirectorySource
from auryn.ingestors.k8s.git_sync import sync_at
from auryn.ingestors.k8s.stitch import stitch

_MANIFEST = (
    "apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: api, namespace: shop}\n"
    "spec:\n  template:\n    spec:\n      containers:\n        - {name: api, image: shop/api:1.0}\n"
    "---\n"
    "apiVersion: networking.k8s.io/v1\nkind: Ingress\nmetadata: {name: ing, namespace: shop}\n"
    "spec:\n  rules:\n    - host: shop.example.com\n      http:\n        paths:\n"
    "          - path: /api\n            backend: {service: {name: %s}}\n"
)


def _ingest_k8s(graph, tmp_path, backend="api", project="shop"):
    (tmp_path / "deploy.yaml").write_text(_MANIFEST % backend)
    src = LocalDirectorySource(str(tmp_path))
    sync_at(GraphWriter(graph), src, src.tree_sha(), project_name=project, repo_uri="t", ref=None)


def _q(graph, c):
    return graph.query(c).result_set


def test_serves_links_backend_workload_to_route_endpoint(graph, tmp_path):
    writer = GraphWriter(graph)
    _ingest_k8s(graph, tmp_path, backend="api")
    report = stitch(writer)
    assert report is not None and report["serves_edges"] == 1
    # the route-keyed APIEndpoint is the HTTP-seam node, SERVES'd by the backing Deployment
    ep = endpoint_placeholder_key("ANY", "/api")
    assert _q(graph, "MATCH (s:Service {name:'api'})-[:SERVES]->(e:APIEndpoint) RETURN e.id") == [[ep]]
    assert _q(graph, "MATCH (e:APIEndpoint) RETURN e.route") == [["/api"]]


_SELECTOR_MANIFEST = (
    "apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: api-deploy, namespace: shop}\n"
    "spec:\n  template:\n    metadata:\n      labels: {app: api}\n"
    "    spec:\n      containers:\n        - {name: c, image: shop/api:1.0}\n"
    "---\n"
    "apiVersion: v1\nkind: Service\nmetadata: {name: api-svc, namespace: shop}\nspec:\n  selector: {app: api}\n"
    "---\n"
    "apiVersion: networking.k8s.io/v1\nkind: Ingress\nmetadata: {name: ing, namespace: shop}\n"
    "spec:\n  rules:\n    - host: shop.example.com\n      http:\n        paths:\n"
    "          - path: /api\n            backend: {service: {name: api-svc}}\n"
)


def test_serves_resolves_backend_through_service_selector(graph, tmp_path):
    # Ingress backend `api-svc` (a Service) selects Deployment `api-deploy` by label -- the names DON'T
    # match, so this exercises selector resolution, not the name fallback.
    writer = GraphWriter(graph)
    (tmp_path / "deploy.yaml").write_text(_SELECTOR_MANIFEST)
    src = LocalDirectorySource(str(tmp_path))
    sync_at(GraphWriter(graph), src, src.tree_sha(), project_name="shop", repo_uri="t", ref=None)
    stitch(writer)
    assert _q(graph, "MATCH (s:Service {name:'api-deploy'})-[:SERVES]->(:APIEndpoint {route:'/api'}) RETURN count(*)") == [[1]]
    # the host-specific Ingress ROUTES to the same endpoint, carrying the host on the edge
    assert _q(graph, "MATCH (:Ingress)-[r:ROUTES]->(:APIEndpoint {route:'/api'}) RETURN r.host") == [["shop.example.com"]]


def test_no_serves_when_backend_has_no_workload(graph, tmp_path):
    writer = GraphWriter(graph)
    _ingest_k8s(graph, tmp_path, backend="does-not-exist")
    stitch(writer)
    assert _q(graph, "MATCH ()-[r:SERVES]->() RETURN count(r)") == [[0]]


def test_stitch_is_idempotent(graph, tmp_path):
    writer = GraphWriter(graph)
    _ingest_k8s(graph, tmp_path)
    stitch(writer)
    first = _q(graph, "MATCH ()-[r:SERVES]->() RETURN count(r)")[0][0]
    stitch(writer)
    assert _q(graph, "MATCH ()-[r:SERVES]->() RETURN count(r)")[0][0] == first == 1


def test_end_to_end_serves_survives_http_gc(graph, tmp_path):
    """Full pipeline: the k8s producer + deploy-k8s create the SERVES edge and route endpoint, and the
    http-endpoints GC (which runs after) must NOT delete an Ingress-served endpoint."""
    (tmp_path / "k8s").mkdir()
    (tmp_path / "k8s" / "deploy.yaml").write_text(_MANIFEST % "api")
    report = ingest.ingest_directory(GraphWriter(graph), str(tmp_path), project_name="shop")
    assert "deploy-k8s" in report.enrichments
    # the SERVES edge and its endpoint survived the http-endpoints orphan GC
    assert _q(graph, "MATCH (:Service {name:'api'})-[:SERVES]->(e:APIEndpoint {route:'/api'}) RETURN count(*)") == [[1]]
