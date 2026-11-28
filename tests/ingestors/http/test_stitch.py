"""Integration tests for the HTTP call<->endpoint stitch (ADR 0006) against the real `graph`
fixture. Seeds a tiny caller-placeholder + C# controller graph directly (no engines needed),
then runs the stitch and checks the EXPOSES_ENDPOINT seam, order-independence, 1-to-many, the
[ActionName] path, the Web API real-endpoint match, and idempotency."""

from __future__ import annotations

from auryn.core.graph_client import GraphWriter
from auryn.ingestors.http.stitch import stitch


def _seed_caller(graph, route="/widgets/list", method="GET"):
    eid = f"extern://http/{method}:{route}"
    graph.query(
        "CREATE (caller:Function {id:'ts://ui/module/api.js.func.load', name:'load'}), "
        "(e:APIEndpoint {id:$eid, route:$route, http_method:$m, resolved:false, "
        "provenance:'http-derived', name:$name}) "
        "CREATE (caller)-[:CALLS_API {provenance:'http-derived'}]->(e)",
        {"eid": eid, "route": route, "m": method, "name": f"{method} {route}"},
    )
    return eid


def _seed_controller(graph, proj="csharp://mono"):
    cid = f"{proj}/ns.class.widgetscontroller"
    mid = f"{cid}.method.list()"
    graph.query(
        "CREATE (c:Class {id:$cid, name:'WidgetsController'}), (m:Method {id:$mid, name:'List'}) "
        "CREATE (c)-[:HAS_METHOD]->(m)",
        {"cid": cid, "mid": mid},
    )
    return mid


def test_stitch_resolves_to_mvc_action_and_completes_trace(graph):
    _seed_caller(graph)
    _seed_controller(graph)

    report = stitch(GraphWriter(graph))
    assert report["resolved"] == 1 and report["unresolved_routes"] == [] and report["exposes_edges"] == 1

    assert graph.query(
        "MATCH (m:Method {name:'List'})-[r:EXPOSES_ENDPOINT {provenance:'http-stitch'}]->(e:APIEndpoint) "
        "RETURN e.resolved"
    ).result_set == [[True]]
    # The golden path now connects end to end across the two subgraphs.
    assert graph.query(
        "MATCH (:Function {name:'load'})-[:CALLS_API]->(:APIEndpoint)<-[:EXPOSES_ENDPOINT]-(:Method {name:'List'}) "
        "RETURN 1"
    ).result_set == [[1]]


def test_stitch_is_order_independent(graph):
    _seed_caller(graph)  # caller ingested first, no controller yet
    r1 = stitch(GraphWriter(graph))
    assert r1["resolved"] == 0 and r1["unresolved_routes"] == ["/widgets/list"]

    _seed_controller(graph)  # controller repo ingested later
    r2 = stitch(GraphWriter(graph))
    assert r2["resolved"] == 1
    assert graph.query(
        "MATCH (:Method {name:'List'})-[:EXPOSES_ENDPOINT]->(:APIEndpoint {resolved:true}) RETURN 1"
    ).result_set == [[1]]


def test_stitch_one_to_many_across_repos(graph):
    _seed_caller(graph)
    _seed_controller(graph, proj="csharp://mono")
    _seed_controller(graph, proj="csharp://platform")  # same action exists as a platform service

    report = stitch(GraphWriter(graph))
    assert report["resolved"] == 1 and report["ambiguous_routes"] == ["/widgets/list"]
    assert graph.query(
        "MATCH (:APIEndpoint {provenance:'http-derived'})<-[:EXPOSES_ENDPOINT]-(m:Method) RETURN count(m)"
    ).result_set == [[2]]


def test_stitch_resolves_via_actionname_override(graph):
    _seed_caller(graph)  # hits /widgets/list
    cid = "csharp://mono/ns.class.widgetscontroller"
    graph.query(
        "CREATE (c:Class {id:$cid, name:'WidgetsController'}), "
        "(m:Method {id:$mid, name:'GetAll', action_name:'List'}) CREATE (c)-[:HAS_METHOD]->(m)",
        {"cid": cid, "mid": cid + ".method.getall()"},
    )
    report = stitch(GraphWriter(graph))
    assert report["resolved"] == 1
    assert graph.query(
        "MATCH (m:Method {name:'GetAll'})-[:EXPOSES_ENDPOINT]->(:APIEndpoint) RETURN 1"
    ).result_set == [[1]]


def test_stitch_matches_web_api_real_endpoint_by_route(graph):
    _seed_caller(graph, route="/api/widgets", method="GET")  # convention (api,widgets) won't match
    graph.query(
        "CREATE (c:Class {id:'csharp://svc/ns.class.widgetsapi', name:'WidgetsApiController'}), "
        "(m:Method {id:'csharp://svc/ns.class.widgetsapi.method.get()', name:'Get'}), "
        "(real:APIEndpoint {id:'csharp://svc/ep', route:'api/widgets', http_method:'GET'}) "
        "CREATE (c)-[:HAS_METHOD]->(m) CREATE (m)-[:EXPOSES_ENDPOINT]->(real)"
    )
    report = stitch(GraphWriter(graph))
    assert report["resolved"] == 1
    assert graph.query(
        "MATCH (:Method {name:'Get'})-[:EXPOSES_ENDPOINT {provenance:'http-stitch'}]->"
        "(:APIEndpoint {provenance:'http-derived'}) RETURN 1"
    ).result_set == [[1]]


def test_stitch_matches_web_api_route_template(graph):
    # /api/widgets/5 must match the attribute-route template api/widgets/{id}.
    _seed_caller(graph, route="/api/widgets/5", method="GET")
    graph.query(
        "CREATE (c:Class {id:'csharp://svc/ns.class.widgetsapi', name:'WidgetsApiController'}), "
        "(m:Method {id:'csharp://svc/ns.class.widgetsapi.method.getbyid(int)', name:'GetById'}), "
        "(real:APIEndpoint {id:'csharp://svc/ep1', route:'api/widgets/{id}', http_method:'GET'}) "
        "CREATE (c)-[:HAS_METHOD]->(m) CREATE (m)-[:EXPOSES_ENDPOINT]->(real)"
    )
    report = stitch(GraphWriter(graph))
    assert report["resolved"] == 1
    assert graph.query(
        "MATCH (:Method {name:'GetById'})-[:EXPOSES_ENDPOINT {provenance:'http-stitch'}]->"
        "(:APIEndpoint {provenance:'http-derived'}) RETURN 1"
    ).result_set == [[1]]


def test_stitch_respects_http_verb(graph):
    # A GET call must NOT bind to a POST-only endpoint on the same route.
    _seed_caller(graph, route="/api/orders", method="GET")
    graph.query(
        "CREATE (c:Class {id:'csharp://svc/ns.class.ordersapi', name:'OrdersApiController'}), "
        "(m:Method {id:'csharp://svc/ns.class.ordersapi.method.create(o)', name:'Create'}), "
        "(real:APIEndpoint {id:'csharp://svc/ep2', route:'api/orders', http_method:'POST'}) "
        "CREATE (c)-[:HAS_METHOD]->(m) CREATE (m)-[:EXPOSES_ENDPOINT]->(real)"
    )
    report = stitch(GraphWriter(graph))
    assert report["resolved"] == 0 and report["unresolved_routes"] == ["/api/orders"]


def test_stitch_idempotent_rebuilds_own_layer(graph):
    _seed_caller(graph)
    _seed_controller(graph)
    stitch(GraphWriter(graph))
    stitch(GraphWriter(graph))  # re-run recomputes its own layer
    assert graph.query(
        "MATCH ()-[r:EXPOSES_ENDPOINT {provenance:'http-stitch'}]->() RETURN count(r)"
    ).result_set == [[1]]


def test_stitch_noop_when_no_placeholders(graph):
    _seed_controller(graph)  # graph has content, but no http-derived placeholders to resolve
    assert stitch(GraphWriter(graph)) is None


def test_declaration_and_call_converge_on_one_node_no_duplicate(graph):
    # ADR 0006 dedup: an attribute-routed declaration and a caller that reach the same route+verb
    # ingest to the SAME route-keyed id, so they arrive already merged (one node). The stitch must
    # keep it a single node, mark it resolved, and NOT overwrite the declared EXPOSES with an
    # http-stitch one (which the next _clear would then delete, breaking the link).
    eid = "extern://http/GET:/api/widgets"
    graph.query(
        "CREATE (caller:Function {id:'ts://ui/load', name:'load'}) "
        "CREATE (c:Class {id:'csharp://svc/wc', name:'WidgetsController'}) "
        "CREATE (m:Method {id:'csharp://svc/get', name:'Get'}) "
        "CREATE (e:APIEndpoint {id:$eid, route:'/api/widgets', http_method:'GET', provenance:'http-derived'}) "
        "CREATE (c)-[:HAS_METHOD]->(m) "
        "CREATE (m)-[:EXPOSES_ENDPOINT]->(e) "  # the declaration's own edge -- no http-stitch provenance
        "CREATE (caller)-[:CALLS_API]->(e)",
        {"eid": eid},
    )
    stitch(GraphWriter(graph))

    assert graph.query("MATCH (e:APIEndpoint {route:'/api/widgets'}) RETURN count(e)").result_set == [[1]]
    # Resolved, and the declared EXPOSES survived untouched (no http-stitch edge for the exact merge).
    assert graph.query(
        "MATCH (:Method {name:'Get'})-[r:EXPOSES_ENDPOINT]->(e:APIEndpoint {id:$eid, resolved:true}) "
        "RETURN coalesce(r.provenance, 'none')", {"eid": eid}
    ).result_set == [["none"]]
    assert graph.query(
        "MATCH ()-[r:EXPOSES_ENDPOINT {provenance:'http-stitch'}]->() RETURN count(r)"
    ).result_set == [[0]]
    # The golden path is intact through the single node.
    assert graph.query(
        "MATCH (:Function {name:'load'})-[:CALLS_API]->(:APIEndpoint {id:$eid})"
        "<-[:EXPOSES_ENDPOINT]-(:Method {name:'Get'}) RETURN 1", {"eid": eid}
    ).result_set == [[1]]


def test_stitch_gcs_orphaned_endpoint(graph):
    # A route-keyed endpoint left with neither caller nor handler (e.g. the route was deleted from the
    # code) can't be reclaimed by any single project's resync -- the stitch GCs it. A still-called or
    # still-declared endpoint must survive.
    orphan = "extern://http/GET:/api/gone"
    graph.query(
        "CREATE (:APIEndpoint {id:$orphan, route:'/api/gone', http_method:'GET', provenance:'http-derived'})",
        {"orphan": orphan},
    )
    _seed_caller(graph)  # a live, still-called placeholder that must NOT be collected
    _seed_controller(graph)
    report = stitch(GraphWriter(graph))

    assert report["orphans_removed"] == 1
    assert graph.query("MATCH (e:APIEndpoint {id:$orphan}) RETURN count(e)", {"orphan": orphan}).result_set == [[0]]
    assert graph.query("MATCH (e:APIEndpoint {route:'/widgets/list'}) RETURN count(e)").result_set == [[1]]


def test_stitch_gcs_endpoint_whose_last_caller_is_gone(graph):
    # Regression: the GC ran after resolving, so the EXPOSES edge this hook had just re-added to the
    # handler kept an endpoint alive once its caller was removed (or its repo forgotten).
    eid = _seed_caller(graph)
    _seed_controller(graph)
    assert stitch(GraphWriter(graph))["resolved"] == 1

    graph.query("MATCH (f:Function {id:'ts://ui/module/api.js.func.load'}) DETACH DELETE f")
    report = stitch(GraphWriter(graph))

    assert report["orphans_removed"] == 1 and report["exposes_edges"] == 0
    assert graph.query("MATCH (e:APIEndpoint {id:$eid}) RETURN count(e)", {"eid": eid}).result_set == [[0]]


def _seed_python_endpoint(graph, route="/charge", method="POST", proj="python://platform"):
    """A pyframework-style DECLARED endpoint: a Function handler (not a Method) EXPOSES a
    `pyapi://` APIEndpoint. This is how Python FastAPI/Flask routes land in the graph."""
    hid = f"{proj}/module/api.func.charge_endpoint"
    eid = f"pyapi://{hid}#{method}:{route}"
    graph.query(
        "CREATE (h:Function {id:$hid, name:'charge_endpoint'}), "
        "(e:APIEndpoint {id:$eid, route:$route, http_method:$m, framework:'fastapi', "
        "provenance:'pyframework', name:$name}) "
        "CREATE (h)-[:EXPOSES_ENDPOINT]->(e)",
        {"hid": hid, "eid": eid, "route": route, "m": method, "name": f"{method} {route}"},
    )
    return hid, eid


def test_stitch_resolves_caller_to_python_function_endpoint(graph):
    """A cross-language HTTP caller (C#/TS -> Python) must heal onto a FastAPI route whose handler is
    a Function, not a Method. Regression: the real-endpoint index used to match only (:Method), so
    a call into a Python service stayed forever unresolved even though pyframework had declared the
    endpoint."""
    placeholder = _seed_caller(graph, route="/charge", method="POST")
    hid, _ = _seed_python_endpoint(graph, route="/charge", method="POST")

    report = stitch(GraphWriter(graph))
    assert report["resolved"] == 1 and report["unresolved_routes"] == []

    # the Function handler now EXPOSES the caller's placeholder, and the placeholder is resolved
    assert graph.query(
        "MATCH (h:Function {id:$hid})-[:EXPOSES_ENDPOINT {provenance:'http-stitch'}]->"
        "(e:APIEndpoint {id:$eid, resolved:true}) RETURN 1",
        {"hid": hid, "eid": placeholder},
    ).result_set == [[1]]
    # golden path connects end to end: caller -> placeholder <- Python function handler
    assert graph.query(
        "MATCH (:Function {name:'load'})-[:CALLS_API]->(:APIEndpoint {id:$eid})"
        "<-[:EXPOSES_ENDPOINT]-(:Function {name:'charge_endpoint'}) RETURN 1",
        {"eid": placeholder},
    ).result_set == [[1]]
