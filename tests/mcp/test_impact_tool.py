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


def test_impact_of_diff_tool_scopes_its_own_git_diff_to_the_repo(falkordb_connection, graph, tmp_path, monkeypatch):
    """With no `diff`, the tool runs git in the project folder, so it knows which repo the paths
    are in: in a graph where two repos both have `src/app.py`, only the one indexed from that
    folder is matched."""
    import subprocess

    repo = tmp_path / "alpha"
    (repo / "src").mkdir(parents=True)
    def git(*a): subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    git("init"); git("config", "user.email", "t@t"); git("config", "user.name", "t")
    (repo / "src" / "app.py").write_text("def run():\n    return 1\n")
    git("add", "."); git("commit", "-m", "init")
    (repo / "src" / "app.py").write_text("def run():\n    return 2\n")
    graph.query(
        f"CREATE (:Project {{id:'python://alpha', name:'alpha', repo_uri:'{repo.resolve().as_posix()}'}}),"
        "(:Project {id:'python://beta', name:'beta', repo_uri:'/elsewhere/beta'}),"
        "(ma:Module {id:'python://alpha/module/src.app', file_path:'src/app.py'}),"
        "(mb:Module {id:'python://beta/module/src.app', file_path:'src/app.py'}),"
        "(fa:Function {id:'python://alpha/module/src.app.func.run()', name:'run_alpha', line_start:1, line_end:2}),"
        "(fb:Function {id:'python://beta/module/src.app.func.run()', name:'run_beta', line_start:1, line_end:2}) "
        "CREATE (ma)-[:HAS_FUNCTION]->(fa), (mb)-[:HAS_FUNCTION]->(fb)"
    )
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo / "src"))  # a subfolder: paths are the top's
    server = create_server(falkordb_connection, graph.name)

    content = _call_tool(server, "impact_of_diff", {}).structured_content

    assert [s["name"] for s in content["changed_symbols"]] == ["run_alpha"]
