"""Integration tests for GraphWriter's vector-index surface against an embedded FalkorDB.

Skipped when the embedded extra (falkordblite) isn't installed. Exercises create_vector_index,
write_embeddings (spanning multiple node kinds via the shared Embeddable label), knn_search
(nearest-first with the real kind label + projected props), clear_embeddings, and the graceful
empty result when no vector index exists yet.
"""

from __future__ import annotations

import pytest

pytest.importorskip("redislite.falkordb_client", reason="falkordblite (embedded extra) not installed")

from auryn.core.graph_client import FalkorDBConfig, GraphWriter, connect_client


@pytest.fixture
def writer(tmp_path):
    config = FalkorDBConfig(embedded=True, db_path=str(tmp_path / "vec.db"), graph_name="vec_test")
    graph = connect_client(config).select_graph(config.graph_name)
    return GraphWriter(graph)


def _seed(writer):
    writer.query(
        "CREATE (:Function {id:'f1', name:'connect', source:'def connect(): ...'}), "
        "(:Class {id:'c1', name:'Widget', source:'class Widget: ...'})"
    )


def test_knn_search_without_index_returns_empty(writer):
    _seed(writer)
    assert writer.knn_search([1.0, 0.0, 0.0], 5) == []


def test_write_and_search_spans_kinds(writer):
    _seed(writer)
    writer.create_vector_index(writer.SHARED_EMBEDDING_LABEL, writer.EMBEDDING_PROPERTY, dim=3)
    writer.write_embeddings(
        [
            ("Function", "f1", [1.0, 0.0, 0.0], None),
            ("Class", "c1", [0.0, 1.0, 0.0], None),
        ]
    )
    hits = writer.knn_search([1.0, 0.0, 0.0], 5, return_properties=("name",))
    assert [h["id"] for h in hits] == ["f1", "c1"]  # nearest first
    assert hits[0]["kind"] == "Function" and hits[0]["name"] == "connect"
    assert hits[1]["kind"] == "Class"
    assert hits[0]["score"] < hits[1]["score"]


def test_create_vector_index_is_idempotent(writer):
    _seed(writer)
    writer.create_vector_index(writer.SHARED_EMBEDDING_LABEL, writer.EMBEDDING_PROPERTY, dim=3)
    # second call must not raise
    writer.create_vector_index(writer.SHARED_EMBEDDING_LABEL, writer.EMBEDDING_PROPERTY, dim=3)


def test_clear_embeddings_removes_from_index(writer):
    _seed(writer)
    writer.create_vector_index(writer.SHARED_EMBEDDING_LABEL, writer.EMBEDDING_PROPERTY, dim=3)
    writer.write_embeddings([("Function", "f1", [1.0, 0.0, 0.0], None)])
    assert [h["id"] for h in writer.knn_search([1.0, 0.0, 0.0], 5)] == ["f1"]
    writer.clear_embeddings([("Function", "f1")])
    assert writer.knn_search([1.0, 0.0, 0.0], 5) == []
    # the marker label + embedding property are gone
    rows = writer.ro_query("MATCH (n:Function {id:'f1'}) RETURN n.embedding").result_set
    assert rows[0][0] is None


def test_drop_vector_index_then_search_degrades(writer):
    _seed(writer)
    writer.create_vector_index(writer.SHARED_EMBEDDING_LABEL, writer.EMBEDDING_PROPERTY, dim=3)
    writer.write_embeddings([("Function", "f1", [1.0, 0.0, 0.0], None)])
    writer.drop_vector_index(writer.SHARED_EMBEDDING_LABEL, writer.EMBEDDING_PROPERTY)
    assert writer.knn_search([1.0, 0.0, 0.0], 5) == []
    writer.drop_vector_index(writer.SHARED_EMBEDDING_LABEL, writer.EMBEDDING_PROPERTY)  # idempotent


def test_write_embeddings_skips_when_fingerprint_changed(writer):
    """The `_fingerprint` guard: a vector built against now-stale content (the node changed and a
    resync re-fingerprinted it) is dropped rather than marking the node done -- so a concurrent
    background embed can't resurrect a stale vector the resync just invalidated."""
    writer.query("CREATE (:Function {id:'f1', name:'connect', _fingerprint:'fp-new'})")
    writer.create_vector_index(writer.SHARED_EMBEDDING_LABEL, writer.EMBEDDING_PROPERTY, dim=3)

    # Vector computed against the OLD content (fp-old); the node now carries fp-new -> not written.
    writer.write_embeddings([("Function", "f1", [1.0, 0.0, 0.0], "fp-old")])
    assert writer.knn_search([1.0, 0.0, 0.0], 5) == []

    # The matching fingerprint writes normally.
    writer.write_embeddings([("Function", "f1", [1.0, 0.0, 0.0], "fp-new")])
    assert [h["id"] for h in writer.knn_search([1.0, 0.0, 0.0], 5)] == ["f1"]


def test_write_embeddings_never_overwrites_a_vector(writer):
    """A pass that scanned a node before another pass embedded it brings a vector for the same
    content; writing it again would only replace the node's index entry (see write_embeddings)."""
    _seed(writer)
    writer.create_vector_index(writer.SHARED_EMBEDDING_LABEL, writer.EMBEDDING_PROPERTY, dim=3)
    writer.write_embeddings([("Function", "f1", [1.0, 0.0, 0.0], None)])

    writer.write_embeddings([("Function", "f1", [0.0, 1.0, 0.0], None)])

    rows = writer.ro_query("MATCH (n:Function {id:'f1'}) RETURN n.embedding").result_set
    assert list(rows[0][0]) == [1.0, 0.0, 0.0]


def test_knn_search_returns_k_past_dead_index_entries(writer):
    """FalkorDB 4.18 keeps a deleted node's vector in the index and spends a KNN slot on it before
    leaving it out. With a deleted twin beside every live node, a plain query for `k` returned k/2."""
    writer.query(
        "UNWIND range(1, 10) AS i "
        "CREATE (:Function {id: 'live' + toString(i)}), (:Function {id: 'dead' + toString(i)})"
    )
    writer.create_vector_index(writer.SHARED_EMBEDDING_LABEL, writer.EMBEDDING_PROPERTY, dim=3)
    vectors = {i: [1.0, i / 10, 0.5] for i in range(1, 11)}
    writer.write_embeddings(
        [("Function", f"{kind}{i}", vec, None) for kind in ("live", "dead") for i, vec in vectors.items()]
    )
    writer.query("MATCH (n:Function) WHERE n.id STARTS WITH 'dead' DETACH DELETE n")

    hits = writer.knn_search([1.0, 0.5, 0.5], 4)

    assert len(hits) == 4 and all(h["id"].startswith("live") for h in hits)
    assert len(writer.knn_search([1.0, 0.5, 0.5], 20)) == 10  # never more than the live nodes
