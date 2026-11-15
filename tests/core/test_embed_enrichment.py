"""Type member-signature enrichment and container (file/module) symbol-inventory embedding.
Uses a capturing fake embedder to assert what text actually gets embedded. Embedded FalkorDB.
"""

from __future__ import annotations

import pytest

pytest.importorskip("redislite.falkordb_client", reason="falkordblite (embedded extra) not installed")

from auryn.core.embeddings.worker import embed_graph, reembed_all
from auryn.core.graph_client import FalkorDBConfig, GraphWriter, connect_client


class CapturingEmbedder:
    """Records every document it's asked to embed, so tests can assert on the assembled text."""

    model_id = "capture"
    dim = 3

    def __init__(self):
        self.docs: list[str] = []

    def embed(self, texts):
        self.docs.extend(texts)
        return [[float(len(t)), 1.0, 2.0] for t in texts]

    def embed_query(self, text):
        return [float(len(text)), 1.0, 2.0]


@pytest.fixture
def writer(tmp_path):
    config = FalkorDBConfig(embedded=True, db_path=str(tmp_path / "enrich.db"), graph_name="enrich_test")
    return GraphWriter(connect_client(config).select_graph(config.graph_name))


def _doc_for(cap, needle):
    return next((d for d in cap.docs if needle in d), None)


def test_type_document_enriched_with_member_signatures(writer):
    writer.query(
        "CREATE (t:Class {id:'c1', name:'Widget', qualified_name:'ui.Widget', doc:'A widget.', "
        "source:'class Widget { }'}), "
        "(m1:Method {id:'m1', name:'draw', signature:'draw() -> void', source:'...'}), "
        "(m2:Method {id:'m2', name:'paint', signature:'paint(Color) -> void', source:'...'}), "
        "(f1:Field {id:'fld1', name:'size'}) "
        "CREATE (t)-[:HAS_METHOD]->(m1), (t)-[:HAS_METHOD]->(m2), (t)-[:HAS_FIELD]->(f1)"
    )
    cap = CapturingEmbedder()
    embed_graph(writer, cap)

    class_doc = _doc_for(cap, "ui.Widget")
    assert class_doc is not None
    assert "class Widget" in class_doc          # declaration header
    assert "draw() -> void" in class_doc        # member signature
    assert "paint(Color) -> void" in class_doc
    assert "size" in class_doc                  # field name (no signature -> name)
    assert "{ }" not in class_doc               # raw body was NOT embedded (header only)


def test_container_document_is_symbol_inventory(writer):
    writer.query(
        "CREATE (mod:Module {id:'app.db', name:'db', qualified_name:'app.db', doc:'DB access.'}), "
        "(fn:Function {id:'app.db.connect', name:'connect', source:'def connect(): ...'}), "
        "(cls:Class {id:'app.db.Conn', name:'Connection', source:'class Connection: ...'}) "
        "CREATE (mod)-[:HAS_FUNCTION]->(fn), (mod)-[:HAS_CLASS]->(cls)"
    )
    cap = CapturingEmbedder()
    embed_graph(writer, cap)

    mod_doc = _doc_for(cap, "app.db\n")
    assert mod_doc is not None
    assert "DB access." in mod_doc
    assert "connect" in mod_doc and "Connection" in mod_doc  # inventory of contained symbols


def test_container_inventory_via_defined_in(writer):
    # Go/Java shape: a type declares itself in a File via DEFINED_IN (incoming edge).
    writer.query(
        "CREATE (f:File {id:'pkg/store.go', name:'store.go'}), "
        "(s:Struct {id:'pkg.Store', name:'Store', source:'type Store struct {}'}) "
        "CREATE (s)-[:DEFINED_IN]->(f)"
    )
    cap = CapturingEmbedder()
    embed_graph(writer, cap)
    file_doc = _doc_for(cap, "store.go")
    assert file_doc is not None and "Store" in file_doc


def test_bare_container_is_skipped(writer):
    writer.query("CREATE (:File {id:'empty.py', name:'empty.py'})")  # no children, no doc
    cap = CapturingEmbedder()
    embed_graph(writer, cap)
    assert not any("empty.py" in d for d in cap.docs)  # nothing worth embedding
    assert writer.ro_query("MATCH (n:File {id:'empty.py'}) RETURN n.embedding").result_set[0][0] is None


def test_reembed_all_recomputes_everything(writer):
    writer.query(
        "CREATE (:Function {id:'f1', name:'connect', source:'def connect(): ...'}), "
        "(:Function {id:'f2', name:'close', source:'def close(): ...'})"
    )
    embed_graph(writer, CapturingEmbedder())
    assert writer.ro_query("MATCH (n:Function) WHERE n.embedding IS NOT NULL RETURN count(n)").result_set[0][0] == 2

    cap = CapturingEmbedder()
    stats = reembed_all(writer, cap)  # drop index + clear all + recompute
    assert stats.embedded == 2 and len(cap.docs) == 2  # everything re-embedded, not skipped as cached
