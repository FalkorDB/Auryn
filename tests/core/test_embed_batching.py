"""Memory-aware embedding batching (issue #835). fastembed pads a batch to its longest document and
transformer attention costs batch*heads*L^2, so a fixed 128-count batch could allocate hundreds of GB
when one long function body lands in it. `_memory_aware_batches` bounds len(batch)*longest^2 and
`_embed_batch` halves on an allocation failure. Pure logic -- no DB, no model download."""

from __future__ import annotations

import pytest

from auryn.core.embeddings import worker
from auryn.core.embeddings.documents import MAX_CHARS


def _docs(specs):
    # specs: list of (id, char_len) -> the (kind, id, doc, fingerprint) tuple shape the worker uses
    return [("Function", i, "x" * n, None) for i, n in specs]


def test_memory_aware_batches_caps_the_window_per_batch():
    budget = 500_000_000  # 5e8
    docs = _docs([(f"s{i}", 100) for i in range(50)] + [("long", 10_000)])

    batches = list(worker._memory_aware_batches(docs, char_budget=budget, max_count=128))

    for b in batches:
        longest = max(len(d[2]) for d in b)
        assert len(b) * longest * longest <= budget            # every batch stays under the window cap
    assert sorted(d[1] for b in batches for d in b) == sorted(d[1] for d in docs)  # all docs, once each
    long_batch = next(b for b in batches if any(d[1] == "long" for d in b))
    assert len(long_batch) * 10_000 * 10_000 <= budget         # the long doc isn't padded with the 50 shorts


def test_memory_aware_batches_respects_the_count_cap():
    docs = _docs([(f"s{i}", 10) for i in range(300)])           # tiny docs -> budget allows many
    batches = list(worker._memory_aware_batches(docs, char_budget=10**12, max_count=128))
    assert all(len(b) <= 128 for b in batches)
    assert sum(len(b) for b in batches) == 300


def test_single_longest_document_fits_a_batch_alone():
    docs = _docs([("big", MAX_CHARS)])
    batches = list(worker._memory_aware_batches(docs, char_budget=worker._batch_char_budget(), max_count=128))
    assert batches == [docs]                                    # default budget >= MAX_CHARS**2


def test_embed_batch_halves_on_allocation_failure_and_preserves_order():
    class _FlakyEmbedder:
        def __init__(self):
            self.calls = []

        def embed(self, texts):
            self.calls.append(len(texts))
            if len(texts) > 2:
                raise MemoryError("onnxruntime failed to allocate")
            return [[float(len(t))] for t in texts]

    emb = _FlakyEmbedder()
    docs = _docs([(f"d{i}", i + 1) for i in range(5)])          # lengths 1..5

    vectors = worker._embed_batch(emb, docs)

    assert vectors == [[1.0], [2.0], [3.0], [4.0], [5.0]]       # all embedded, original order preserved
    assert emb.calls[0] == 5 and len(emb.calls) > 1            # tried the full batch, then halved and retried
    assert min(emb.calls) <= 2                                  # some retry succeeded at a small-enough size


def test_embed_batch_reraises_at_single_doc_and_on_programming_errors():
    class _AlwaysOOM:
        def embed(self, texts):
            raise MemoryError("nope")

    with pytest.raises(MemoryError):
        worker._embed_batch(_AlwaysOOM(), _docs([("d", 5)]))    # one doc can't be halved -> re-raise

    class _Buggy:
        def embed(self, texts):
            raise TypeError("a real bug, not memory")

    with pytest.raises(TypeError):
        worker._embed_batch(_Buggy(), _docs([("a", 1), ("b", 2)]))  # programming error is not retried
