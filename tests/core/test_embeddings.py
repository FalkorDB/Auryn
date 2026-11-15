"""Unit tests for the embeddings foundation: the embedder abstraction and the document
assembler. No fastembed model is downloaded -- the FastEmbedEmbedder test injects a fake model
so only the vector-shaping/plumbing logic is exercised.
"""

from __future__ import annotations

from auryn.core.embeddings import build_document, get_embedder, known_dim
from auryn.core.embeddings.documents import EMBEDDABLE_LABELS, is_embeddable
from auryn.core.embeddings.embedder import DEFAULT_LOCAL_DIM, DEFAULT_LOCAL_MODEL, FastEmbedEmbedder


# -- embedder --------------------------------------------------------------------------------


class _FakeModel:
    """Stand-in for fastembed.TextEmbedding: one deterministic 3-dim vector per input."""

    def __init__(self):
        self.embed_calls = []

    def embed(self, texts):
        texts = list(texts)
        self.embed_calls.append(texts)
        for i, _ in enumerate(texts):
            yield [float(i), 1.0, 2.0]


def test_get_embedder_defaults():
    emb = get_embedder()
    assert emb.model_id == DEFAULT_LOCAL_MODEL
    assert emb.dim == DEFAULT_LOCAL_DIM == 768


def test_known_dim_static_table():
    assert known_dim("jinaai/jina-embeddings-v2-base-code") == 768
    assert known_dim("BAAI/bge-small-en-v1.5") == 384


def test_fastembed_embed_shapes_vectors(monkeypatch):
    emb = FastEmbedEmbedder()
    fake = _FakeModel()
    monkeypatch.setattr(emb, "_ensure_model", lambda: fake)
    out = emb.embed(["a", "b"])
    assert out == [[0.0, 1.0, 2.0], [1.0, 1.0, 2.0]]
    # plain python floats, not numpy scalars
    assert all(isinstance(x, float) for v in out for x in v)


def test_fastembed_embed_empty_is_noop():
    emb = FastEmbedEmbedder()
    # must not touch the model at all for an empty batch
    assert emb.embed([]) == []


def test_fastembed_embed_query_falls_back_to_embed(monkeypatch):
    emb = FastEmbedEmbedder()
    fake = _FakeModel()  # no query_embed attribute -> symmetric fallback
    monkeypatch.setattr(emb, "_ensure_model", lambda: fake)
    assert emb.embed_query("hello") == [0.0, 1.0, 2.0]


# -- document assembler ----------------------------------------------------------------------


def test_embeddable_labels_cover_callables_types_containers():
    for label in ("Function", "Method", "Class", "Struct", "Interface", "File", "Module", "Package"):
        assert is_embeddable(label)
    assert not is_embeddable("Parameter")
    assert not is_embeddable("Field")


def test_callable_document_is_doc_plus_source():
    doc = build_document(
        "Function",
        {"qualified_name": "app.db.connect", "doc": "Open a connection.", "source": "def connect():\n    return db()"},
    )
    assert doc == "app.db.connect\nOpen a connection.\ndef connect():\n    return db()"


def test_callable_document_without_doc():
    doc = build_document("Method", {"name": "run", "source": "void run() {}"})
    assert doc == "run\nvoid run() {}"


def test_type_document_uses_header_not_raw_body():
    # A class's raw source inlines method bodies; the document must NOT include them.
    src = "class Widget(Base) {\n  void draw() { paint(); }\n}"
    doc = build_document("Class", {"qualified_name": "ui.Widget", "doc": "A widget.", "source": src})
    assert "paint()" not in doc
    assert doc == "ui.Widget\nA widget.\nclass Widget(Base)"


def test_type_document_python_braceless_header():
    doc = build_document("Class", {"name": "Widget", "source": "class Widget(Base):\n    def draw(self): ..."})
    assert doc == "Widget\nclass Widget(Base):"


def test_type_document_with_member_signatures():
    doc = build_document(
        "Interface",
        {"qualified_name": "svc.Repo", "source": "interface Repo {}"},
        members=["Get(id) -> Item", "Put(item)"],
    )
    assert doc == "svc.Repo\ninterface Repo\nGet(id) -> Item\nPut(item)"


def test_container_document_is_inventory():
    doc = build_document(
        "Module",
        {"qualified_name": "app.db", "doc": "DB access."},
        members=["connect", "Connection", "execute"],
    )
    assert doc == "app.db\nDB access.\nconnect\nConnection\nexecute"


def test_non_embeddable_returns_none():
    assert build_document("Parameter", {"name": "x"}) is None


def test_empty_node_returns_none():
    assert build_document("Function", {}) is None


def test_source_is_truncated_to_budget():
    big = "x" * 50_000
    doc = build_document("Function", {"name": "f", "doc": "d", "source": big})
    assert len(doc) <= 20_000
    assert doc.startswith("f\nd\n")
