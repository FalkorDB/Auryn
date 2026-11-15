"""Tests for the Python web-framework enrichment (route decorators -> APIEndpoint +
EXPOSES_ENDPOINT) and the endpoint-aware trace_entrypoint it feeds.

`parse_routes` is pure/offline. The graph tests seed a tiny handler+callee subgraph
directly (no full ingest / no Jedi) so they're fast, and assert the enrichment is
idempotent, provenance-scoped (leaves foreign APIEndpoints alone), and that an APIEndpoint
is now a real trace_entrypoint entry point reaching its handler's call closure.
"""

from __future__ import annotations

import pytest

from auryn.core import graph_queries as gq
from auryn.core.graph_client import GraphWriter
from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.ingestors.pyframework.synthesize import RouteInfo, parse_routes, run

# -- parser (offline) --------------------------------------------------------------


def test_parse_fastapi_verb_decorators():
    assert parse_routes("app.post('/widgets')") == [RouteInfo("POST", "/widgets", "fastapi")]
    assert parse_routes("router.get('/items/{id}')") == [RouteInfo("GET", "/items/{id}", "fastapi")]


def test_parse_flask_route_with_methods():
    assert parse_routes("app.route('/x', methods=['GET', 'POST'])") == [
        RouteInfo("GET", "/x", "flask"),
        RouteInfo("POST", "/x", "flask"),
    ]
    # bare @app.route defaults to GET
    assert parse_routes("bp.route('/y')") == [RouteInfo("GET", "/y", "flask")]


def test_non_route_decorators_are_ignored():
    assert parse_routes("staticmethod") == []
    assert parse_routes("dataclass") == []
    assert parse_routes("app.middleware('http')") == []  # a method call, but not a route verb
    assert parse_routes("property") == []


# -- enrichment against a live graph -----------------------------------------------

_HANDLER = "python://demo/module/app.api.func.create_widget(name)"
_CALLEE = "python://demo/module/app.db.func.get_connection()"


@pytest.fixture
def seeded(graph):
    """A handler Function decorated with a route, calling a project function."""
    w = GraphWriter(graph)
    w.merge_nodes(
        [
            GraphNode("Function", {"id": _HANDLER}, {"name": "create_widget", "decorators": ["app.post('/widgets')"]}),
            GraphNode("Function", {"id": _CALLEE}, {"name": "get_connection", "decorators": []}),
        ]
    )
    w.merge_edges([GraphEdge("CALLS", "Function", {"id": _HANDLER}, "Function", {"id": _CALLEE}, {"resolution": "resolved"})])
    return graph, w


def test_run_synthesizes_endpoint_and_edge(seeded):
    graph, w = seeded
    report = run(w, None, None)
    assert report == {"endpoints": 1, "handlers": 1}
    assert graph.query(
        "MATCH (h:Function {name:'create_widget'})-[:EXPOSES_ENDPOINT]->(e:APIEndpoint) "
        "RETURN e.http_method, e.route, e.framework, e.provenance"
    ).result_set == [["POST", "/widgets", "fastapi", "pyframework"]]


def test_run_is_idempotent(seeded):
    graph, w = seeded
    run(w, None, None)
    run(w, None, None)
    assert graph.query("MATCH (e:APIEndpoint) RETURN count(e)").result_set == [[1]]


def test_clear_leaves_foreign_endpoints_alone(seeded):
    graph, w = seeded
    # a C#-style APIEndpoint (no pyframework provenance) must survive our recompute
    w.merge_nodes([GraphNode("APIEndpoint", {"id": "csharp://x/endpoint"}, {"route": "api/other", "http_method": "GET"})])
    run(w, None, None)
    assert graph.query("MATCH (e:APIEndpoint {id:'csharp://x/endpoint'}) RETURN 1").result_set == [[1]]
    assert graph.query("MATCH (e:APIEndpoint {provenance:'pyframework'}) RETURN count(e)").result_set == [[1]]


def test_endpoint_is_a_trace_entrypoint_entry_point(seeded):
    graph, w = seeded
    run(w, None, None)
    endpoint_id = graph.query("MATCH (e:APIEndpoint {provenance:'pyframework'}) RETURN e.id").result_set[0][0]

    bi = gq.trace_entrypoint(graph, endpoint_id)
    assert bi is not None
    assert bi["entry"]["label"] == "APIEndpoint"
    # the closure reaches the handler AND what the handler calls
    names = {f["name"] for f in bi["functions"]}
    assert {"create_widget", "get_connection"} <= names
