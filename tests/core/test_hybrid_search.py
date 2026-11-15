"""Hybrid retrieval: RRF fusion (pure) + full-text (lexical) search + hybrid_search fusing
lexical and vector against an embedded FalkorDB. A deterministic fake embedder keeps the vector
side reproducible (no model download)."""

from __future__ import annotations

import pytest

from auryn.core.embeddings.search import hybrid_search, rrf_fuse

falkordb = pytest.importorskip("redislite.falkordb_client", reason="falkordblite (embedded extra) not installed")

from auryn.core.embeddings.worker import embed_graph
from auryn.core.graph_client import FalkorDBConfig, GraphWriter, connect_client


# -- rrf_fuse (pure) -------------------------------------------------------------------------


def test_rrf_rewards_agreement():
    # 'a' is in BOTH lists (#1 and #2); 'c' is #1 in only one; 'b' is #2 in only one.
    # Presence in both lists beats a single top-1 vote.
    fused = rrf_fuse([["a", "b"], ["c", "a"]], k=1)
    ids = [i for i, _ in fused]
    assert ids[0] == "a"          # corroborated by both rankers -> wins over c's single #1
    assert set(ids) == {"a", "b", "c"}


def test_rrf_absent_from_a_list_contributes_nothing():
    fused = dict(rrf_fuse([["x"], ["y"]], k=60))
    assert fused["x"] == pytest.approx(1 / 61)
    assert fused["y"] == pytest.approx(1 / 61)


def test_rrf_empty():
    assert rrf_fuse([[], []]) == []


# -- integration -----------------------------------------------------------------------------


class FakeEmbedder:
    """3-dim toy space: [db/connection, auth/user, query] keyword presence."""

    model_id = "fake"
    dim = 3

    def _vec(self, t: str):
        t = t.lower()
        return [
            1.0 if ("connect" in t or "database" in t) else 0.0,
            1.0 if ("user" in t or "auth" in t) else 0.0,
            1.0 if ("query" in t or "sql" in t) else 0.0,
        ]

    def embed(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)


@pytest.fixture
def writer(tmp_path):
    cfg = FalkorDBConfig(embedded=True, db_path=str(tmp_path / "hy.db"), graph_name="hy")
    return GraphWriter(connect_client(cfg).select_graph(cfg.graph_name))


def _seed(writer):
    writer.query(
        "CREATE "
        "(:Function {id:'connect', name:'connect', qualified_name:'app.db.connect', doc:'Open a pooled connection to the Postgres database.', source:'def connect(): ...'}),"
        "(:Class {id:'Connection', name:'Connection', qualified_name:'app.db.Connection', doc:'A live database connection handle.', source:'class Connection: ...'}),"
        "(:Function {id:'login', name:'login', qualified_name:'app.auth.login', doc:'Authenticate a user and return a session token.', source:'def login(): ...'}),"
        "(:Function {id:'run_query', name:'run_query', qualified_name:'app.db.run_query', doc:'Execute a SQL query.', source:'def run_query(): ...'})"
    )


def test_fulltext_search_ranks_by_keywords(writer):
    _seed(writer)
    embed_graph(writer, FakeEmbedder())  # creates both the vector AND the full-text index
    hits = writer.fulltext_search("database connection", 5, return_properties=("name",))
    ids = [h["id"] for h in hits]
    assert "connect" in ids and "Connection" in ids
    assert "login" not in ids  # no keyword overlap
    assert all("score" in h and "kind" in h for h in hits)


def test_fulltext_search_empty_without_index(writer):
    _seed(writer)  # no embed pass -> no full-text index
    assert writer.fulltext_search("database", 5) == []


def test_hybrid_fuses_lexical_and_vector(writer):
    _seed(writer)
    embed_graph(writer, FakeEmbedder())
    hits = hybrid_search(writer, FakeEmbedder(), "open a database connection", k=4)
    ids = [h["id"] for h in hits]
    assert set(ids[:2]) == {"connect", "Connection"}  # corroborated by both rankers -> top
    assert all("score" in h for h in hits)


def test_hybrid_degrades_to_lexical_without_embedder(writer):
    _seed(writer)
    embed_graph(writer, FakeEmbedder())
    hits = hybrid_search(writer, None, "database connection", k=4)  # embedder=None -> lexical only
    assert "connect" in [h["id"] for h in hits]


def test_hybrid_degrades_to_vector_without_fulltext_index(writer):
    # Build only the vector index (no full-text) -> hybrid must still work off the vector ranker.
    _seed(writer)
    emb = FakeEmbedder()
    docs = [("Function", "connect", emb.embed(["connect database"])[0])]
    writer.write_embeddings(docs)
    writer.create_vector_index(writer.SHARED_EMBEDDING_LABEL, writer.EMBEDDING_PROPERTY, emb.dim)
    assert writer.fulltext_search("database", 5) == []  # confirm no full-text index
    hits = hybrid_search(writer, emb, "database connection", k=4)
    assert "connect" in [h["id"] for h in hits]
