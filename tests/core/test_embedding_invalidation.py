"""Embeddings stay fresh across resyncs: when a node's content changes, its stale embedding is
cleared so the embed worker recomputes it -- while unchanged nodes keep theirs. Covers both write
paths: graph_sync.stream_apply (every git-language recompute-and-diff) and
GraphWriter.apply_delta (the scoped incremental engine). Embedded FalkorDB + a fake embedder.
"""

from __future__ import annotations

import pytest

pytest.importorskip("redislite.falkordb_client", reason="falkordblite (embedded extra) not installed")

from auryn.core.embeddings.worker import embed_graph
from auryn.core.graph_client import FalkorDBConfig, GraphWriter, connect_client
from auryn.core.graph_model import GraphNode, NodeSchema
from auryn.core.graph_sync import compute_delta, stream_apply


class FakeEmbedder:
    model_id = "fake-test-model"
    dim = 3

    def embed(self, texts):
        return [[float(len(t)), 1.0, 2.0] for t in texts]

    def embed_query(self, text):
        return [float(len(text)), 1.0, 2.0]


@pytest.fixture
def writer(tmp_path):
    config = FalkorDBConfig(embedded=True, db_path=str(tmp_path / "inv.db"), graph_name="inv_test")
    return GraphWriter(connect_client(config).select_graph(config.graph_name))


def _embedding(writer, node_id):
    return writer.ro_query(f"MATCH (n:Function {{id:'{node_id}'}}) RETURN n.embedding").result_set[0][0]


def test_stream_apply_clears_only_changed_node_embedding(writer):
    def make(src_f1):
        def gen():
            yield GraphNode("Function", {"id": "p/f1"}, {"name": "f", "source": src_f1, "doc": ""})
            yield GraphNode("Function", {"id": "p/f2"}, {"name": "g", "source": "stable", "doc": ""})
        return gen

    writer.setup_schema([NodeSchema("Function")])  # create the graph + id index, as a real sync does
    stream_apply(writer, "p/", ["Function"], make("version-one"))
    embed_graph(writer, FakeEmbedder())
    assert _embedding(writer, "p/f1") is not None
    assert _embedding(writer, "p/f2") is not None

    # Resync: only f1's source changes.
    stats, _, _ = stream_apply(writer, "p/", ["Function"], make("version-two-different"))
    assert stats.nodes_updated == 1
    assert _embedding(writer, "p/f1") is None  # stale embedding cleared -> re-queued
    assert _embedding(writer, "p/f2") is not None  # unchanged -> embedding kept

    # The cleared node is re-embedded on the next embed pass; the untouched one is not re-done.
    stats2 = embed_graph(writer, FakeEmbedder())
    assert stats2.scanned == 1 and stats2.embedded == 1
    assert _embedding(writer, "p/f1") is not None


def test_apply_delta_clears_changed_node_embedding(writer):
    writer.merge_nodes([GraphNode("Function", {"id": "p/f1"}, {"name": "f", "source": "v1", "doc": ""})])
    embed_graph(writer, FakeEmbedder())
    assert _embedding(writer, "p/f1") is not None

    cur_nodes, cur_edges = writer.read_fingerprints("p/", ["Function"])
    target = [GraphNode("Function", {"id": "p/f1"}, {"name": "f", "source": "v2-changed", "doc": ""})]
    delta = compute_delta(cur_nodes, cur_edges, target)
    assert len(delta.nodes_to_upsert) == 1  # content changed -> upsert

    writer.apply_delta(delta)
    assert _embedding(writer, "p/f1") is None  # cleared by apply_delta's invalidation


def test_apply_delta_noop_on_unchanged_node_keeps_embedding(writer):
    writer.merge_nodes([GraphNode("Function", {"id": "p/f1"}, {"name": "f", "source": "v1", "doc": ""})])
    embed_graph(writer, FakeEmbedder())

    cur_nodes, cur_edges = writer.read_fingerprints("p/", ["Function"])
    # Same content -> compute_delta yields no upserts -> apply_delta clears nothing.
    target = [GraphNode("Function", {"id": "p/f1"}, {"name": "f", "source": "v1", "doc": ""})]
    delta = compute_delta(cur_nodes, cur_edges, target)
    assert delta.nodes_to_upsert == []

    writer.apply_delta(delta)
    assert _embedding(writer, "p/f1") is not None  # untouched
