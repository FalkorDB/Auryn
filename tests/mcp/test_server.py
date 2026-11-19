"""Integration test: seed a small graph directly, then drive the MCP server's
tools through a real in-process MCP client (mcp.client.Client over the SDK's
in-memory transport) -- exercises the actual protocol layer (tool listing,
schema validation, structured content, error surfacing), not just the
underlying graph_queries functions tests/core/test_graph_queries.py already
covers.

No pytest-asyncio/anyio-pytest dependency added for this -- asyncio.run() around
a small async helper is enough for a handful of tests and needs no new
dependency or plugin configuration.
"""

from __future__ import annotations

import asyncio

import git
import pytest
from mcp.client import Client

from auryn.mcp.server import create_server


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    """Point the usage log (core/usage.py) at a throwaway home so driving the MCP tools in these
    tests never writes to the real ~/.auryn/usage.jsonl."""
    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "home"))


def _seed(graph) -> None:
    graph.query(
        """
        CREATE (a:Procedure {id: 'a', name: 'TransferEmployee'}),
               (b:Procedure {id: 'b', name: 'LogChange'}),
               (t:Table {id: 't', name: 'AUDIT_LOG'})
        CREATE (a)-[:CALLS]->(b), (b)-[:WRITES]->(t)
        """
    )


def _call_tool(server, tool_name: str, arguments: dict):
    async def run():
        async with Client(server) as client:
            return await client.call_tool(tool_name, arguments)

    return asyncio.run(run())


def _list_tool_names(server) -> set[str]:
    async def run():
        async with Client(server) as client:
            return await client.list_tools()

    return {t.name for t in asyncio.run(run()).tools}


def test_server_advertises_its_tools(falkordb_connection, graph):
    _seed(graph)
    server = create_server(falkordb_connection, graph.name)

    assert _list_tool_names(server) == {
        "auryn_status",
        "list_graphs",
        "use_graph",
        "index_repository",
        "ingest_repository",
        "locate",
        "search_code",
        "get_context",
        "get_schema",
        "who_calls",
        "callees",
        "find_path",
        "impact_of",
        "impact_of_diff",
        "trace_entrypoint",
        "run_cypher",
    }


def test_locate_finds_seeded_node(falkordb_connection, graph):
    _seed(graph)
    server = create_server(falkordb_connection, graph.name)

    result = _call_tool(server, "locate", {"name": "Transfer"})

    assert result.is_error is False
    assert result.structured_content["graph"] == graph.name
    assert result.structured_content["results"][0]["name"] == "TransferEmployee"


def test_auryn_status_reports_contents(falkordb_connection, graph):
    _seed(graph)
    server = create_server(falkordb_connection, graph.name)

    content = _call_tool(server, "auryn_status", {}).structured_content
    assert content["has_data"] is True
    assert content["node_count"] == 3


def test_list_graphs_reports_repositories_across_graphs(falkordb_connection, graph):
    # The bound graph represents ONE repo...
    graph.query(
        "CREATE (:Project {id:'p1', name:'orders-service', repo_uri:'/repos/orders', "
        "depth:'fast', base_commit:'abc'})"
    )
    # ...a second graph represents TWO repos indexed together (co-located so cross-repo calls
    # resolve) -- exactly the "one graph, several repositories" case list_graphs must surface.
    other = falkordb_connection.select_graph(f"{graph.name}_other")
    other.query(
        "CREATE (:Project {id:'p2', name:'invoicing-service', repo_uri:'/repos/invoicing', "
        "depth:'deep', base_commit:'def'}), "
        "(:Project {id:'p3', name:'acme-payments', repo_uri:'/repos/payments', "
        "depth:'deep', base_commit:'ghi'})"
    )
    try:
        server = create_server(falkordb_connection, graph.name)
        content = _call_tool(server, "list_graphs", {}).structured_content

        assert content["current"] == graph.name
        by_name = {g["name"]: g for g in content["graphs"]}  # instance may hold other graphs too

        bound = by_name[graph.name]
        assert bound["current"] is True and bound["node_count"] == 1
        assert bound["repositories"] == [
            {"name": "orders-service", "repo_uri": "/repos/orders", "depth": "fast", "base_commit": "abc"}
        ]

        multi = by_name[other.name]
        assert multi["current"] is False and multi["node_count"] == 2
        # several repositories, returned sorted by name
        assert [r["name"] for r in multi["repositories"]] == ["acme-payments", "invoicing-service"]
        assert {r["depth"] for r in multi["repositories"]} == {"deep"}
    finally:
        other.delete()


def test_who_calls_and_callees(falkordb_connection, graph):
    _seed(graph)
    server = create_server(falkordb_connection, graph.name)

    callers = _call_tool(server, "who_calls", {"node_id": "b"}).structured_content
    assert {n["name"] for n in callers["nodes"]} == {"TransferEmployee"}


def test_tool_calls_are_recorded_as_usage(falkordb_connection, graph):
    # Every MCP tool call is an agent accessing the graph -> one usage event, tagged by operation
    # (core/usage.py), the data behind `auryn stats`.
    from auryn.core import usage

    _seed(graph)
    server = create_server(falkordb_connection, graph.name)

    _call_tool(server, "locate", {"name": "Transfer"})              # read, ok, has results
    _call_tool(server, "who_calls", {"node_id": "b"})               # read, ok
    _call_tool(server, "get_context", {"node_id": "nope"})          # read, errors (bad id)

    events = usage.read_events(graph=graph.name)
    by_tool = {e["tool"]: e for e in events}
    assert by_tool["locate"]["op"] == "read" and by_tool["locate"]["status"] == "ok"
    assert by_tool["locate"]["result_size"] >= 1
    assert by_tool["who_calls"]["status"] == "ok"
    assert by_tool["get_context"]["status"] == "error"             # the finally-block still records it

    summary = usage.summarize(events)
    assert summary["total"] >= 3 and summary["errors"] >= 1 and summary["by_op"]["read"] >= 3

    downstream = _call_tool(server, "callees", {"node_id": "a"}).structured_content
    assert {n["name"] for n in downstream["nodes"]} == {"LogChange"}


def test_find_path_call_chain_via_edge_types(falkordb_connection, graph):
    # edge_types=["CALLS"] restricts to a pure call chain (what call_path used to do).
    _seed(graph)
    server = create_server(falkordb_connection, graph.name)

    content = _call_tool(
        server, "find_path", {"from_id": "a", "to_id": "b", "edge_types": ["CALLS"]}
    ).structured_content
    assert content["found"] is True
    assert [n["name"] for n in content["nodes"]] == ["TransferEmployee", "LogChange"]


def test_find_path_crosses_non_call_edges(falkordb_connection, graph):
    # a -CALLS-> b -WRITES-> t : a CALLS-only search can't reach the table (WRITES
    # isn't a CALL), but the default (any edge type) connects them.
    _seed(graph)
    server = create_server(falkordb_connection, graph.name)

    content = _call_tool(server, "find_path", {"from_id": "a", "to_id": "t"}).structured_content
    assert content["found"] is True
    assert [n["name"] for n in content["nodes"]] == ["TransferEmployee", "LogChange", "AUDIT_LOG"]
    assert [e["type"] for e in content["edges"]] == ["CALLS", "WRITES"]


def test_find_path_direction_matters(falkordb_connection, graph):
    _seed(graph)
    server = create_server(falkordb_connection, graph.name)

    # nothing points from the table back up to the procedure...
    directed = _call_tool(server, "find_path", {"from_id": "t", "to_id": "a"}).structured_content
    assert directed["found"] is False
    # ...but undirected walks the edges backwards and connects them.
    undirected = _call_tool(
        server, "find_path", {"from_id": "t", "to_id": "a", "directed": False}
    ).structured_content
    assert undirected["found"] is True
    assert undirected["hops"] == 2


def test_get_schema_and_run_cypher(falkordb_connection, graph):
    _seed(graph)
    server = create_server(falkordb_connection, graph.name)

    schema = _call_tool(server, "get_schema", {}).structured_content
    assert "Procedure" in {row["label"] for row in schema["node_labels"]}
    assert "CALLS" in {row["type"] for row in schema["edge_types"]}
    # the schema graph (meta-model): (Procedure)-[CALLS]->(Procedure), (Procedure)-[WRITES]->(Table)
    patterns = {(p["from"], p["type"], p["to"]) for p in schema["relationship_patterns"]}
    assert ("Procedure", "CALLS", "Procedure") in patterns
    assert ("Procedure", "WRITES", "Table") in patterns
    assert "name" in schema["properties_by_label"]["Procedure"]

    cy = _call_tool(server, "run_cypher", {"query": "MATCH (n:Table) RETURN n"}).structured_content
    assert {n["name"] for n in cy["nodes"]} == {"AUDIT_LOG"}


def test_get_context_returns_node_and_neighbors(falkordb_connection, graph):
    _seed(graph)
    server = create_server(falkordb_connection, graph.name)

    result = _call_tool(server, "get_context", {"node_id": "a"})

    content = result.structured_content
    assert content["center"]["name"] == "TransferEmployee"
    assert {n["name"] for n in content["nodes"]} == {"LogChange"}


def test_impact_of_downstream_from_entry(falkordb_connection, graph):
    _seed(graph)
    server = create_server(falkordb_connection, graph.name)

    result = _call_tool(server, "impact_of", {"node_id": "a", "direction": "downstream"})

    names = {i["node"]["name"] for i in result.structured_content["impacted"]}
    assert names == {"LogChange", "AUDIT_LOG"}


def test_trace_entrypoint_crosses_into_table(falkordb_connection, graph):
    _seed(graph)
    server = create_server(falkordb_connection, graph.name)

    result = _call_tool(server, "trace_entrypoint", {"entry_id": "a"})

    content = result.structured_content
    assert content["functions"][0]["name"] == "LogChange"
    assert content["tables_written"][0]["name"] == "AUDIT_LOG"
    assert content["validations"] is None
    assert content["validations_note"]


def test_missing_node_surfaces_as_a_tool_error_not_a_crash(falkordb_connection, graph):
    _seed(graph)
    server = create_server(falkordb_connection, graph.name)

    result = _call_tool(server, "get_context", {"node_id": "does-not-exist"})

    assert result.is_error is True
    assert "does-not-exist" in result.content[0].text


def test_invalid_direction_surfaces_as_a_tool_error(falkordb_connection, graph):
    _seed(graph)
    server = create_server(falkordb_connection, graph.name)

    result = _call_tool(server, "impact_of", {"node_id": "a", "direction": "sideways"})

    assert result.is_error is True


# -- per-operation graph selection (an agent switches graphs at will) --------------------


def _seed_named(conn, name: str, proc: str):
    g = conn.select_graph(name)
    g.query("CREATE (:Procedure {id:'x', name:$n})", {"n": proc})
    return g


def test_per_operation_graph_argument_targets_another_graph(falkordb_connection, graph):
    # Default graph holds one repo's data; a SECOND graph on the same instance holds another's.
    _seed(graph)  # default: TransferEmployee/LogChange
    other = _seed_named(falkordb_connection, f"{graph.name}_other", "OtherProc")
    try:
        server = create_server(falkordb_connection, graph.name)

        # No graph arg -> the default graph.
        here = _call_tool(server, "locate", {"name": "Transfer"}).structured_content
        assert here["graph"] == graph.name
        assert here["results"][0]["name"] == "TransferEmployee"

        # graph=<other> -> that graph, in the SAME session, no restart.
        there = _call_tool(server, "locate", {"name": "Other", "graph": other.name}).structured_content
        assert there["graph"] == other.name
        assert there["results"][0]["name"] == "OtherProc"

        # ...and the default is unchanged afterwards.
        assert _call_tool(server, "locate", {"name": "Transfer"}).structured_content["graph"] == graph.name
    finally:
        other.delete()


def test_use_graph_changes_the_session_default(falkordb_connection, graph):
    _seed(graph)
    other = _seed_named(falkordb_connection, f"{graph.name}_def", "OtherProc")
    try:
        server = create_server(falkordb_connection, graph.name)

        switched = _call_tool(server, "use_graph", {"name": other.name}).structured_content
        assert switched["graph"] == other.name and switched["default"] is True

        # subsequent no-arg calls now hit `other`
        assert _call_tool(server, "locate", {"name": "Other"}).structured_content["results"][0]["name"] == "OtherProc"
        assert _call_tool(server, "list_graphs", {}).structured_content["current"] == other.name
    finally:
        other.delete()


def test_reading_an_unknown_graph_is_an_error_not_an_empty_result(falkordb_connection, graph):
    # A typo'd graph name must fail loudly rather than silently return "nothing here".
    _seed(graph)
    server = create_server(falkordb_connection, graph.name)

    result = _call_tool(server, "locate", {"name": "Transfer", "graph": "no-such-graph-xyz"})
    assert result.is_error is True
    assert "no-such-graph-xyz" in result.content[0].text


def test_use_graph_rejects_unknown_name(falkordb_connection, graph):
    _seed(graph)
    server = create_server(falkordb_connection, graph.name)

    assert _call_tool(server, "use_graph", {"name": "nope-not-here"}).is_error is True


def test_auryn_status_on_empty_graph_hints_to_switch(falkordb_connection, graph):
    # The default graph is empty but others exist -> status must nudge toward list_graphs/use_graph
    # (the exact situation that stalled the audit: bound to the wrong, empty graph).
    server = create_server(falkordb_connection, graph.name)  # graph seeded with nothing
    content = _call_tool(server, "auryn_status", {}).structured_content
    assert content["has_data"] is False
    assert "list_graphs" in content["hint"]


def test_index_into_a_new_graph_name_creates_it(falkordb_connection, graph, tmp_path):
    # A write to an unknown graph name creates it -- an agent can isolate a repo without a new server.
    root = tmp_path / "IsolatedRepo"
    _make_mixed_dir(root)
    new_name = f"{graph.name}_isolated"
    server = create_server(falkordb_connection, graph.name)
    try:
        content = _call_tool(server, "index_repository", {"path": str(root), "graph": new_name}).structured_content
        assert content["graph"] == new_name
        assert content["totals"]["nodes_added"] > 0
        # landed in the NEW graph...
        assert falkordb_connection.select_graph(new_name).query(
            "MATCH (c:Class {name:'Widget'}) RETURN c"
        ).result_set != []
        # ...and NOT in the default graph.
        assert graph.query("MATCH (c:Class {name:'Widget'}) RETURN c").result_set == []
    finally:
        falkordb_connection.select_graph(new_name).delete()


def _make_mixed_repo(root):
    repo = git.Repo.init(str(root), initial_branch="main")
    with repo.config_writer() as cw:
        cw.set_value("user", "email", "t@e.com")
        cw.set_value("user", "name", "T")
    (root / "src").mkdir(parents=True)
    (root / "src" / "Widget.cs").write_text("namespace A { public class Widget { public int Id {get;set;} } }")
    (root / "db").mkdir()
    (root / "db" / "hr.pkb").write_text(
        "CREATE OR REPLACE PACKAGE BODY hr IS PROCEDURE go IS BEGIN NULL; END; END hr;"
    )
    repo.index.add(["src/Widget.cs", "db/hr.pkb"])
    return repo.index.commit("init").hexsha


def test_ingest_repository_ingests_and_reports_per_language(falkordb_connection, graph, tmp_path):
    root = tmp_path / "MixedRepo"
    commit = _make_mixed_repo(root)
    server = create_server(falkordb_connection, graph.name)

    result = _call_tool(server, "ingest_repository", {"repo_uri": str(root)})

    assert result.is_error is False
    content = result.structured_content
    assert content["graph"] == graph.name
    assert content["commit_sha"] == commit
    assert set(content["languages_ingested"]) == {"csharp", "plsql"}
    assert content["totals"]["nodes_added"] > 0
    # The write actually landed in this server's graph.
    assert graph.query("MATCH (c:Class {name:'Widget'}) RETURN c").result_set != []
    assert graph.query("MATCH (p:Procedure {name:'GO'}) RETURN p").result_set != []


def test_ingest_repository_bad_repo_surfaces_as_a_tool_error(falkordb_connection, graph, tmp_path):
    server = create_server(falkordb_connection, graph.name)

    result = _call_tool(server, "ingest_repository", {"repo_uri": str(tmp_path / "does-not-exist")})

    assert result.is_error is True


def _make_mixed_dir(root):
    """A plain directory (no git) spanning two ingestable languages."""
    (root / "src").mkdir(parents=True)
    (root / "src" / "Widget.cs").write_text("namespace A { public class Widget { public int Id {get;set;} } }")
    (root / "db").mkdir()
    (root / "db" / "hr.pkb").write_text(
        "CREATE OR REPLACE PACKAGE BODY hr IS PROCEDURE go IS BEGIN NULL; END; END hr;"
    )


def test_ingest_directory_ingests_and_reports_per_language(falkordb_connection, graph, tmp_path):
    root = tmp_path / "LocalMixed"
    _make_mixed_dir(root)
    server = create_server(falkordb_connection, graph.name)

    result = _call_tool(server, "index_repository", {"path": str(root)})

    assert result.is_error is False
    content = result.structured_content
    assert content["graph"] == graph.name
    assert content["project_name"] == "LocalMixed"  # derived from the directory name
    assert set(content["languages_ingested"]) == {"csharp", "plsql"}
    assert content["totals"]["nodes_added"] > 0
    # The write actually landed in this server's graph.
    assert graph.query("MATCH (c:Class {name:'Widget'}) RETURN c").result_set != []
    assert graph.query("MATCH (p:Procedure {name:'GO'}) RETURN p").result_set != []


def test_index_repository_takes_its_turn_with_the_repos_syncs(falkordb_connection, graph, tmp_path, monkeypatch):
    """An agent's `index_repository` writes the project subgraph a commit hook's `auryn sync`
    writes too, so it ingests under the same per-repo lock (core/sync_lock.py) -- and, being an
    explicit request, never as the redundant one that steps aside."""
    import contextlib

    from auryn.core import ingest
    from auryn.core.project import resolve_project_root

    root = tmp_path / "LocalMixed"
    _make_mixed_dir(root)
    events = []

    @contextlib.contextmanager
    def recorded(local_dir, *, queue_one=True):
        events.append(("lock", local_dir, queue_one))
        yield True
        events.append("unlock")

    real_ingest = ingest.ingest_directory
    monkeypatch.setattr("auryn.mcp.server.sync_lock", recorded)
    monkeypatch.setattr(ingest, "ingest_directory", lambda *a, **k: events.append("ingest") or real_ingest(*a, **k))

    result = _call_tool(create_server(falkordb_connection, graph.name), "index_repository", {"path": str(root)})

    assert result.is_error is False
    assert events == [("lock", resolve_project_root(str(root)), False), "ingest", "unlock"]


def test_index_repository_keeps_the_lock_until_a_background_deep_pass_is_done(falkordb_connection, graph, tmp_path, monkeypatch):
    """With `deep="background"` the tool answers on the fast graph while a thread still deepens it.
    That thread writes the same project subgraph, so the lock outlives the call: it is released
    only once the deep pass is joined."""
    import contextlib
    import threading
    from types import SimpleNamespace

    from auryn.core import ingest

    events, deep_done, unlocked = [], threading.Event(), threading.Event()

    @contextlib.contextmanager
    def recorded(local_dir, *, queue_one=True):
        yield True
        events.append("unlock")
        unlocked.set()

    def join_deep_phase():
        deep_done.wait(timeout=10)
        events.append("join")

    monkeypatch.setattr("auryn.mcp.server.sync_lock", recorded)
    monkeypatch.setattr(ingest, "ingest_directory", lambda *a, **k: SimpleNamespace(to_dict=dict))
    monkeypatch.setattr(ingest, "join_deep_phase", join_deep_phase)

    result = _call_tool(
        create_server(falkordb_connection, graph.name), "index_repository", {"path": str(tmp_path), "deep": "background"}
    )

    assert result.is_error is False
    assert events == []  # the tool has answered; the deep pass is still running and the lock still held
    deep_done.set()
    assert unlocked.wait(timeout=10)
    assert events == ["join", "unlock"]


def test_index_repository_bad_path_surfaces_as_a_tool_error(falkordb_connection, graph, tmp_path):
    server = create_server(falkordb_connection, graph.name)

    result = _call_tool(server, "index_repository", {"path": str(tmp_path / "does-not-exist")})

    assert result.is_error is True


def test_mcp_ingest_wires_resync_trigger_like_the_cli(falkordb_connection, graph, tmp_path):
    # Parity: an MCP index must have the SAME effects as a CLI ingest -- register the repo and set
    # up the install-time resync trigger (default: commit -> git hook), not just write the graph.
    from auryn.core import hooks
    from auryn.core.app_config import get_repo
    from auryn.core.project import resolve_project_root

    root = tmp_path / "HookRepo"
    _make_mixed_repo(root)  # a real git repo
    server = create_server(falkordb_connection, graph.name)

    content = _call_tool(server, "index_repository", {"path": str(root)}).structured_content

    # reported back on the tool result...
    assert content["resync"] == {"registered": True, "trigger": "commit", "hooks_installed": True}
    # ...and actually done: the git hook is on disk and the repo is registered to THIS graph.
    local_dir = str(resolve_project_root(str(root)))
    assert hooks.is_installed(local_dir)
    reg = get_repo(local_dir)
    assert reg is not None and reg.graph_name == graph.name
