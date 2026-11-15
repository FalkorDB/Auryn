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
            ("Function", "f1", [1.0, 0.0, 0.0]),
            ("Class", "c1", [0.0, 1.0, 0.0]),
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
    writer.write_embeddings([("Function", "f1", [1.0, 0.0, 0.0])])
    assert [h["id"] for h in writer.knn_search([1.0, 0.0, 0.0], 5)] == ["f1"]
    writer.clear_embeddings([("Function", "f1")])
    assert writer.knn_search([1.0, 0.0, 0.0], 5) == []
    # the marker label + embedding property are gone
    rows = writer.ro_query("MATCH (n:Function {id:'f1'}) RETURN n.embedding").result_set
    assert rows[0][0] is None


def test_drop_vector_index_then_search_degrades(writer):
    _seed(writer)
    writer.create_vector_index(writer.SHARED_EMBEDDING_LABEL, writer.EMBEDDING_PROPERTY, dim=3)
    writer.write_embeddings([("Function", "f1", [1.0, 0.0, 0.0])])
    writer.drop_vector_index(writer.SHARED_EMBEDDING_LABEL, writer.EMBEDDING_PROPERTY)
    assert writer.knn_search([1.0, 0.0, 0.0], 5) == []
    writer.drop_vector_index(writer.SHARED_EMBEDDING_LABEL, writer.EMBEDDING_PROPERTY)  # idempotent
