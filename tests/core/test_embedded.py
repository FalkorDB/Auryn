"""The embedded, serverless FalkorDB path -- connect_client(config) with embedded=True,
backed by the optional `falkordblite` package (bundled Redis + FalkorDB module, in-process,
no server/Docker). Skipped unless falkordblite is importable: it's an optional extra, so a
lean checkout can still run the rest of the suite. The dev group installs it, so CI does
exercise this. Auto-install is force-disabled here so a missing wheel can never turn a unit
test into a surprise network install -- the importorskip already gates presence."""

from __future__ import annotations

import pytest

pytest.importorskip("redislite.falkordb_client", reason="falkordblite (embedded extra) not installed")

from auryn.core.graph_client import FalkorDBConfig, GraphWriter, connect_client
from auryn.core.graph_model import GraphNode, NodeSchema


def test_embedded_connect_and_write_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("AURYN_AUTO_INSTALL", "0")
    db_file = tmp_path / "graph.db"
    config = FalkorDBConfig(embedded=True, db_path=str(db_file), graph_name="embedded_test")

    client = connect_client(config)
    # The whole GraphWriter surface (execute_command for RESULTSET_SIZE, unique constraint +
    # range index, MERGE) must work against the embedded client exactly as against a server.
    writer = GraphWriter(client.select_graph(config.graph_name))
    writer.setup_schema([NodeSchema(label="Widget", key_property="id", indexed_properties=("name",))])
    writer.merge_node(GraphNode("Widget", {"id": "w1"}, {"id": "w1", "name": "Alice"}))

    rows = client.select_graph(config.graph_name).ro_query("MATCH (n:Widget) RETURN n.id, n.name").result_set
    assert rows == [["w1", "Alice"]]


def test_embedded_persists_across_reconnect(tmp_path, monkeypatch):
    """Reusing the same db_path reuses the graph -- what makes a quickstart re-run cheap."""
    monkeypatch.setenv("AURYN_AUTO_INSTALL", "0")
    db_file = tmp_path / "persist.db"
    config = FalkorDBConfig(embedded=True, db_path=str(db_file), graph_name="persist_test")

    writer = GraphWriter(connect_client(config).select_graph(config.graph_name))
    writer.setup_schema([NodeSchema(label="Widget", key_property="id")])
    writer.merge_node(GraphNode("Widget", {"id": "w1"}, {"id": "w1"}))

    # A fresh client on the same file (same process) still sees the node.
    rows = connect_client(config).select_graph(config.graph_name).ro_query("MATCH (n:Widget) RETURN n.id").result_set
    assert rows == [["w1"]]
