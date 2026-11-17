"""End-to-end test of embed_graph against an embedded FalkorDB, using a deterministic fake
embedder (no model download). Verifies: pending nodes get embedded + indexed, KNN then finds
the right node by meaning, a second run is a no-op, and clearing a node re-queues it.
"""

from __future__ import annotations

from types import SimpleNamespace

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


def test_embed_graph_returns_only_once_the_indexes_are_searchable(writer):
    """FalkorDB builds an index in the background after CREATE INDEX returns, and a query against
    one still under construction comes back EMPTY. With more than a handful of nodes that window
    is real (a few ms at 300), so a search right after the embed pass found nothing -- and on a
    loaded CI runner the same race made the three-node search tests fail at random."""
    writer.query(
        "UNWIND range(1, 300) AS i CREATE (:Function {id: 'f' + toString(i), name: 'connect' + toString(i), "
        "qualified_name: 'db.connect' + toString(i), doc: 'Open a db connection.', source: 'def connect(): ...'})"
    )
    emb = FakeEmbedder()

    stats = embed_graph(writer, emb)

    assert stats.embedded == 300 and stats.index_ready
    assert len(writer.knn_search(emb.embed_query("connect"), 2)) == 2
    assert len(writer.fulltext_search("database connection", 5)) == 5


@pytest.mark.parametrize("embedded, index_ready, said", [
    (0, False, "vector index unchanged."),
    (3, True, "vector index ready."),
    (3, False, "vector index not ready yet"),
])
def test_embed_command_says_when_the_index_is_not_ready(monkeypatch, embedded, index_ready, said):
    """`index_ready` is False both when nothing was embedded and when the index couldn't be
    confirmed built. `auryn embed` printed "unchanged" for both -- telling the user all was well
    while searches still came back empty, the very confusion the wait is there to end."""
    from click.testing import CliRunner

    from auryn.cli import cli
    from auryn.core.embeddings.worker import EmbedStats

    stats = EmbedStats(scanned=embedded, embedded=embedded, model_id="fake", dim=8, index_ready=index_ready)
    monkeypatch.setattr("auryn.cli._resolve_connection", lambda *conn: None)
    monkeypatch.setattr("auryn.cli.connect_client", lambda config: SimpleNamespace(select_graph=lambda name: None))
    monkeypatch.setattr("auryn.core.embeddings.get_embedder", lambda model: None)
    monkeypatch.setattr("auryn.core.embeddings.worker.embed_graph", lambda writer, embedder: stats)

    res = CliRunner().invoke(cli, ["embed", "--graph-name", "g"])

    assert res.exit_code == 0, res.output
    assert said in res.output


class _IndexStatuses:
    """Stands in for the graph: each `db.indexes()` poll answers with the next list of statuses
    (the last one repeats), and counts the polls."""

    def __init__(self, *polls):
        self._polls = list(polls)
        self.polled = 0

    def ro_query(self, cypher, params=None):
        self.polled += 1
        statuses = self._polls.pop(0) if len(self._polls) > 1 else self._polls[0]
        return SimpleNamespace(result_set=[[s] for s in statuses])


_BUILDING = "[Indexing] 1/3: UNDER CONSTRUCTION"


def test_await_indexes_polls_until_every_index_is_operational(monkeypatch):
    monkeypatch.setattr("auryn.core.graph_client.time.sleep", lambda s: None)
    # two indexes on the label finishing one after the other: only the last poll has both built
    graph = _IndexStatuses([_BUILDING, _BUILDING], ["OPERATIONAL", _BUILDING], ["OPERATIONAL", "OPERATIONAL"])

    assert GraphWriter(graph).await_indexes("Embeddable") is True
    assert graph.polled == 3


def test_await_indexes_gives_up_at_the_deadline_and_without_an_index():
    still_building = _IndexStatuses([_BUILDING])
    assert GraphWriter(still_building).await_indexes("Embeddable", timeout=0) is False
    assert still_building.polled == 1  # one look, then the deadline -- no endless wait

    no_index = _IndexStatuses([])  # creation failed: nothing is building, nothing will be ready
    assert GraphWriter(no_index).await_indexes("Embeddable") is False


def test_await_indexes_backs_off_and_survives_an_unreadable_state(monkeypatch):
    """A long build is polled ever more slowly (20 ms doubling up to a second), not 50 times a
    second for minutes. And a server that can't answer `db.indexes()` this way means "can't tell",
    reported as not ready -- not an exception after every vector has already been written."""
    from redis.exceptions import ConnectionError as RedisConnectionError
    from redis.exceptions import ResponseError

    pauses = []
    monkeypatch.setattr("auryn.core.graph_client.time.sleep", pauses.append)
    graph = _IndexStatuses(*([[_BUILDING]] * 8), ["OPERATIONAL"])

    assert GraphWriter(graph).await_indexes("Embeddable") is True
    assert pauses == [0.02, 0.04, 0.08, 0.16, 0.32, 0.64, 1.0, 1.0]

    for error in (ResponseError("Procedure `db.indexes` does not yield output `status`"),
                  RedisConnectionError("Connection closed by server.")):
        class _Unreadable:
            def ro_query(self, cypher, params=None, error=error):
                raise error

        assert GraphWriter(_Unreadable()).await_indexes("Embeddable") is False


def test_await_indexes_never_sleeps_past_its_deadline(monkeypatch):
    """A short timeout is respected: the last pause is cut to the time that is left."""
    clock = [0.0]
    pauses = []

    def sleep(seconds):
        pauses.append(round(seconds, 3))
        clock[0] += seconds

    monkeypatch.setattr("auryn.core.graph_client.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("auryn.core.graph_client.time.sleep", sleep)

    assert GraphWriter(_IndexStatuses([_BUILDING])).await_indexes("Embeddable", timeout=0.05) is False
    assert pauses == [0.02, 0.03]  # not 0.02 + 0.04


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
