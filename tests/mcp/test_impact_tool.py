"""The MCP `impact_of_diff` tool, driven through the real in-process MCP client. Uses the raw
`diff` argument so the test needs no git repo -- just a seeded two-service graph."""

from __future__ import annotations

import asyncio

from mcp.client import Client

from auryn.mcp.server import create_server

DIFF = "diff --git a/orders/db.py b/orders/db.py\n--- a/orders/db.py\n+++ b/orders/db.py\n@@ -10,3 +10,2 @@\n-old\n+new\n"


def _call_tool(server, name, args):
    async def run():
        async with Client(server) as c:
            return await c.call_tool(name, args)
    return asyncio.run(run())


def _seed(graph):
    graph.query(
        "CREATE "
        "(gc:Function {id:'orders://orders/db.get_connection', name:'get_connection', file_path:'orders/db.py', line_start:10, line_end:14}),"
        "(place:Function {id:'orders://orders/svc.place_order', name:'place_order', file_path:'orders/svc.py', line_start:20, line_end:40}),"
        "(cancel:Function {id:'orders://orders/svc.cancel_order', name:'cancel_order', file_path:'orders/svc.py', line_start:42, line_end:55}),"
        "(ep:APIEndpoint {id:'orders://orders/ep.post_orders', name:'POST /orders'}),"
        "(charge:Function {id:'billing://billing/svc.charge', name:'charge', file_path:'billing/svc.py', line_start:12, line_end:30}) "
        "CREATE (place)-[:CALLS]->(gc), (cancel)-[:CALLS]->(gc),"
        "(place)-[:EXPOSES_ENDPOINT]->(ep), (charge)-[:CALLS]->(place)"
    )


def test_impact_of_diff_tool_from_raw_diff(falkordb_connection, graph):
    _seed(graph)
    server = create_server(falkordb_connection, graph.name)
    content = _call_tool(server, "impact_of_diff", {"diff": DIFF}).structured_content

    assert content["graph"] == graph.name
    assert [s["name"] for s in content["changed_symbols"]] == ["get_connection"]
    imp = content["impact"]
    assert set(imp["direct_callers"]) == {"place_order", "cancel_order"}
    assert imp["api_endpoints_reached"] == ["POST /orders"]
    assert imp["cross_service_consumers"] == ["billing://billing"]
    assert any("cross-service" in f for f in content["flags"])
    # ids present for pivoting
    assert all("id" in a for a in imp["affected"])


def test_impact_of_diff_tool_empty_diff(falkordb_connection, graph):
    _seed(graph)
    server = create_server(falkordb_connection, graph.name)
    content = _call_tool(server, "impact_of_diff", {"diff": ""}).structured_content
    assert content["impact"]["total_affected"] == 0
    assert "hint" in content
