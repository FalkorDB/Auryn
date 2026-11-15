"""End-to-end test of embed_graph against an embedded FalkorDB, using a deterministic fake
embedder (no model download). Verifies: pending nodes get embedded + indexed, KNN then finds
the right node by meaning, a second run is a no-op, and clearing a node re-queues it.
"""

from __future__ import annotations

import pytest

pytest.importorskip("redislite.falkordb_client", reason="falkordblite (embedded extra) not installed")

from auryn.core.embeddings.worker import embed_graph
from auryn.core.graph_client import FalkorDBConfig, GraphWriter, connect_client


class FakeEmbedder:
    """3-dim toy space: [mentions 'connect', mentions 'widget', bias]. Lets a query land on the
    node whose document shares its keyword."""

    model_id = "fake-test-model"
    dim = 3

    def _vec(self, text: str) -> list[float]:
        t = text.lower()
        return [1.0 if "connect" in t else 0.0, 1.0 if "widget" in t else 0.0, 0.1]

    def embed(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)


@pytest.fixture
def writer(tmp_path):
    config = FalkorDBConfig(embedded=True, db_path=str(tmp_path / "embed.db"), graph_name="embed_test")
    return GraphWriter(connect_client(config).select_graph(config.graph_name))


def _seed(writer):
    writer.query(
        "CREATE "
        "(:Function {id:'f1', name:'connect', qualified_name:'db.connect', doc:'Open a db connection.', source:'def connect(): ...'}), "
        "(:Class {id:'c1', name:'Widget', qualified_name:'ui.Widget', doc:'A UI widget.', source:'class Widget: ...'}), "
        "(:Parameter {id:'p1', name:'x'})"  # not embeddable
    )


def test_embed_graph_indexes_and_search_finds_by_meaning(writer):
    _seed(writer)
    emb = FakeEmbedder()
    stats = embed_graph(writer, emb)
    assert stats.embedded == 2  # the Function and Class, not the Parameter
    assert stats.scanned == 2
    assert stats.index_ready and stats.dim == 3

    hits = writer.knn_search(emb.embed_query("connect"), 2, return_properties=("name",))
    assert hits[0]["id"] == "f1" and hits[0]["kind"] == "Function"
    hits = writer.knn_search(emb.embed_query("widget"), 2, return_properties=("name",))
    assert hits[0]["id"] == "c1" and hits[0]["kind"] == "Class"


def test_embed_graph_is_idempotent(writer):
    _seed(writer)
    emb = FakeEmbedder()
    embed_graph(writer, emb)
    stats2 = embed_graph(writer, emb)  # nothing pending now
    assert stats2.scanned == 0 and stats2.embedded == 0


def test_cleared_node_is_reembedded(writer):
    _seed(writer)
    emb = FakeEmbedder()
    embed_graph(writer, emb)
    writer.clear_embeddings([("Function", "f1")])
    stats = embed_graph(writer, emb)
    assert stats.scanned == 1 and stats.embedded == 1  # only the cleared node re-embeds


def test_parameter_nodes_are_never_embedded(writer):
    _seed(writer)
    embed_graph(writer, FakeEmbedder())
    rows = writer.ro_query("MATCH (n:Parameter {id:'p1'}) RETURN n.embedding").result_set
    assert rows[0][0] is None
