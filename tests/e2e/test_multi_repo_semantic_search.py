"""Semantic search on a graph that holds several repos returns the `k` results it is asked for.

Each `auryn ingest --graph-name shared ...` asks for an embed pass over the whole shared graph, so
with several repos landing one after another, two passes can overlap: the second scans the same
pending nodes before the first has written their vectors. FalkorDB 4.18 (the build falkordblite
0.10 bundles, which this suite runs on) keeps a node's old vector index entry whenever the node is
written again, and the vector query spends one of its `k` slots on each such entry before leaving
it out. Regression: the second pass used to write every vector again, so search came back with
half of `k`.
"""

from __future__ import annotations

import hashlib
import threading
from uuid import uuid4

import pytest

pytest.importorskip("redislite.falkordb_client", reason="falkordblite (embedded extra) not installed")

from auryn.core.embeddings.worker import embed_graph
from auryn.core.graph_client import GraphWriter
from auryn.core.ingest import ingest_directory


class HashEmbedder:
    """A deterministic 8-dim vector per text (no model download), distinct per document."""

    model_id = "fake-hash-model"
    dim = 8

    def embed(self, texts):
        return [self.embed_query(t) for t in texts]

    def embed_query(self, text):
        digest = hashlib.sha256(text.encode()).digest()
        return [0.05 + b / 255 for b in digest[: self.dim]]


class GatedEmbedder(HashEmbedder):
    """Signals once its pass has scanned (the first embed call comes after the scan), then holds
    the pass until released -- so another pass can run to completion in between."""

    def __init__(self):
        self.scanned = threading.Event()
        self.release = threading.Event()

    def embed(self, texts):
        self.scanned.set()
        assert self.release.wait(timeout=60)
        return super().embed(texts)


def _write_repos(tmp_path):
    billing = tmp_path / "billing"
    (billing / "billing").mkdir(parents=True)
    (billing / "billing" / "__init__.py").write_text("")
    (billing / "billing" / "core.py").write_text(
        'def charge(amount):\n    """Charge a card."""\n    return amount\n\n\n'
        'def refund(amount):\n    """Refund a charge."""\n    return -amount\n'
    )
    webshop = tmp_path / "webshop"
    webshop.mkdir()
    (webshop / "checkout.py").write_text(
        "from billing.core import charge, refund\n\n\n"
        'def place_order(total):\n    """Take payment for an order."""\n    return charge(total)\n\n\n'
        'def cancel_order(total):\n    """Give the money back."""\n    return refund(total)\n'
    )
    return billing, webshop


def _embedded_count(writer):
    return writer.ro_query(f"MATCH (n:{writer.SHARED_EMBEDDING_LABEL}) RETURN count(n)").result_set[0][0]


def test_overlapping_embed_passes_over_a_shared_graph_keep_search_full(falkordb_connection, tmp_path):
    billing, webshop = _write_repos(tmp_path)
    g = falkordb_connection.select_graph(f"multi_repo_search_{uuid4().hex}")
    writer = GraphWriter(g)
    try:
        for repo in (billing, webshop):
            ingest_directory(writer, str(repo), deep="off", auto_install=False, embed="off")

        # The second pass scans while nothing is embedded yet, then waits; the first pass embeds
        # every node and builds the index; only then does the second pass write.
        late = GatedEmbedder()
        second = threading.Thread(target=embed_graph, args=(writer, late))
        second.start()
        assert late.scanned.wait(timeout=60)
        assert embed_graph(writer, HashEmbedder()).index_ready
        late.release.set()
        second.join(timeout=60)
        assert not second.is_alive()

        live = _embedded_count(writer)
        assert live >= 4  # both repos' functions, at least
        hits = writer.knn_search(HashEmbedder().embed_query("charge a card"), live, return_properties=("name",))
        assert len(hits) == live
        assert {"charge", "refund", "place_order", "cancel_order"} <= {h["name"] for h in hits}
        # The second pass found every vector already written and left the index as it was.
        assert writer._vector_index_counts() == (live, live)
    finally:
        g.delete()


def test_search_stays_full_after_one_repo_of_a_shared_graph_is_resynced(falkordb_connection, tmp_path):
    """Resyncing one repo clears and re-embeds its changed nodes and deletes its removed ones. On
    FalkorDB 4.18 each of those leaves a dead entry in the vector index, which Auryn can't avoid --
    search has to look past them."""
    billing, webshop = _write_repos(tmp_path)
    g = falkordb_connection.select_graph(f"multi_repo_resync_{uuid4().hex}")
    writer = GraphWriter(g)
    try:
        for repo in (billing, webshop):
            ingest_directory(writer, str(repo), deep="off", auto_install=False, embed="off")
        embed_graph(writer, HashEmbedder())
        old_charge = writer.ro_query("MATCH (f:Function {name:'charge'}) RETURN f.embedding").result_set[0][0]

        (billing / "billing" / "core.py").write_text(
            'def charge(amount, currency):\n    """Charge a card in a currency."""\n    return amount\n'
        )  # charge changed, refund removed
        ingest_directory(writer, str(billing), deep="off", auto_install=False, embed="off")
        assert embed_graph(writer, HashEmbedder()).index_ready

        live = _embedded_count(writer)
        # charge's old vector is the nearest entry to itself, so it is sure to take a slot.
        hits = writer.knn_search(list(old_charge), live, return_properties=("name",))
        assert len(hits) == live
        assert "refund" not in {h["name"] for h in hits}
    finally:
        g.delete()
