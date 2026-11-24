"""MCP `search_code` tool: driven through the real in-process MCP client, with a deterministic
fake embedder monkeypatched in (no model download). Verifies semantic hits carry pivotable ids,
and that the tool degrades gracefully (empty + hint) when no embeddings exist yet.
"""

from __future__ import annotations

import asyncio

import pytest
from mcp.client import Client

import auryn.mcp.server as server_mod
from auryn.core.embeddings.worker import embed_graph
from auryn.core.graph_client import GraphWriter
from auryn.mcp.server import create_server


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "home"))


class FakeEmbedder:
    model_id = "fake-test-model"
    dim = 3

    def _vec(self, text: str) -> list[float]:
        t = text.lower()
        return [1.0 if "connect" in t else 0.0, 1.0 if "widget" in t else 0.0, 0.1]

    def embed(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)


def _call_tool(server, tool_name: str, arguments: dict):
    async def run():
        async with Client(server) as client:
            return await client.call_tool(tool_name, arguments)

    return asyncio.run(run())


def _seed(graph):
    graph.query(
        "CREATE "
        "(:Function {id:'f1', name:'connect', qualified_name:'db.connect', "
        "doc:'Open a database connection.', source:'def connect(): ...'}), "
        "(:Class {id:'c1', name:'Widget', qualified_name:'ui.Widget', "
        "doc:'A UI widget.', source:'class Widget: ...'})"
    )


def test_search_code_returns_semantic_hits(falkordb_connection, graph, monkeypatch):
    monkeypatch.setattr(server_mod, "get_embedder", lambda *a, **k: FakeEmbedder())
    _seed(graph)
    embed_graph(GraphWriter(graph), FakeEmbedder())  # build vectors + index

    server = create_server(falkordb_connection, graph.name)
    result = _call_tool(server, "search_code", {"query": "how do I connect to the database", "k": 5})

    assert result.is_error is False
    content = result.structured_content
    assert content["graph"] == graph.name
    assert content["mode"] == "hybrid"  # default fuses lexical + vector
    top = content["results"][0]
    assert top["id"] == "f1" and top["kind"] == "Function"
    assert top["qualified_name"] == "db.connect"
    assert "connection" in top["snippet"].lower()
    # id is pivotable into the structural tools
    ctx = _call_tool(server, "get_context", {"node_id": top["id"]})
    assert ctx.is_error is False


def test_search_code_lexical_mode_needs_no_model(falkordb_connection, graph, monkeypatch):
    _seed(graph)
    embed_graph(GraphWriter(graph), FakeEmbedder())  # builds the full-text index too
    # model is now unavailable, but lexical mode must still work (no embedder needed)
    monkeypatch.setattr(server_mod, "get_embedder", lambda *a, **k: (_ for _ in ()).throw(ImportError("x")))
    server = create_server(falkordb_connection, graph.name)
    content = _call_tool(server, "search_code", {"query": "database connection", "mode": "lexical"}).structured_content
    assert content["mode"] == "lexical"
    assert "f1" in [r["id"] for r in content["results"]]


def test_search_code_hybrid_degrades_to_lexical_when_model_unavailable(falkordb_connection, graph, monkeypatch):
    _seed(graph)
    embed_graph(GraphWriter(graph), FakeEmbedder())  # full-text + vector indexes exist
    monkeypatch.setattr(server_mod, "get_embedder", lambda *a, **k: (_ for _ in ()).throw(ImportError("x")))
    server = create_server(falkordb_connection, graph.name)
    # default hybrid: model can't construct -> lexical-only, still returns results (no error/hint)
    content = _call_tool(server, "search_code", {"query": "database connection"}).structured_content
    assert content["mode"] == "hybrid"
    assert "f1" in [r["id"] for r in content["results"]]


def test_search_code_degrades_when_no_embeddings(falkordb_connection, graph, monkeypatch):
    monkeypatch.setattr(server_mod, "get_embedder", lambda *a, **k: FakeEmbedder())
    _seed(graph)  # nodes exist but no embeddings/index built

    server = create_server(falkordb_connection, graph.name)
    content = _call_tool(server, "search_code", {"query": "connect"}).structured_content

    assert content["results"] == []
    assert "hint" in content


def test_search_code_semantic_mode_reports_unavailable_model(falkordb_connection, graph, monkeypatch):
    # mode='semantic' explicitly needs the model, so an unavailable model is surfaced (not hidden
    # behind a lexical fallback as hybrid would do).
    def _boom(*a, **k):
        raise ImportError("fastembed not installed")

    monkeypatch.setattr(server_mod, "get_embedder", _boom)
    _seed(graph)
    server = create_server(falkordb_connection, graph.name)
    content = _call_tool(server, "search_code", {"query": "connect", "mode": "semantic"}).structured_content

    assert content["results"] == []
    assert "unavailable" in content["hint"].lower()


def test_run_cypher_strips_embedding_from_returned_entities(falkordb_connection, graph):
    """Even if an agent bypasses the curated tools and calls run_cypher with `RETURN n`, the raw
    result is reshaped through the LEAN serializer, so the embedding vector never comes back and bulky
    fields (source, >200-char text) are dropped -- `MATCH (n) RETURN n` over many nodes can't flood the
    agent's context. The viewer's own cypher console keeps full nodes; get_context returns source."""
    graph.query(
        "CREATE (:Function {id:'f1', name:'connect', source:'def connect(): ...', "
        "embedding:[0.1, 0.2, 0.3], _fingerprint:'abc'})"
    )
    server = create_server(falkordb_connection, graph.name)

    content = _call_tool(server, "run_cypher", {"query": "MATCH (n:Function) RETURN n"}).structured_content
    node = next(n for n in content["nodes"] if n["id"] == "f1")
    assert node["properties"].get("name") == "connect"      # identifying props preserved
    assert "source" not in node["properties"]               # lean: bulky source dropped (get_context has it)
    assert "embedding" not in node["properties"]            # vector stripped
    assert "_fingerprint" not in node["properties"]

    # A scalar projection of the vector isn't a returned *entity* -- run_cypher keeps graph
    # objects and drops raw scalars, so `RETURN n.embedding` yields no nodes/edges at all.
    scalar = _call_tool(server, "run_cypher", {"query": "MATCH (n:Function) RETURN n.embedding"}).structured_content
    assert scalar["nodes"] == [] and scalar["edges"] == []
