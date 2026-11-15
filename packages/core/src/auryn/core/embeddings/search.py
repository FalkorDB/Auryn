"""Hybrid retrieval: fuse the lexical (full-text / BM25) and vector (semantic) rankers with
Reciprocal Rank Fusion (RRF).

Each ranker is imperfect alone -- lexical misses paraphrase ("verify identity" never matches a
`login` whose text says "authenticate"), vector under-separates exact identifier hits. RRF
combines them using only *ranks*: no incompatible score scales to reconcile, no per-query
tuning. A node both rankers surface gets two contributions and rises to the top; a node only one
finds is still kept (recall preserved). Both rankers run natively against the same FalkorDB nodes
(one store -- full-text index + vector index), so hybrid is a small fusion step, not a second
system.

Degrades gracefully: no embedding model -> lexical-only; no full-text index yet -> vector-only.
"""

from __future__ import annotations

import logging

from auryn.core.embeddings.embedder import Embedder
from auryn.core.graph_client import GraphWriter

logger = logging.getLogger(__name__)

RRF_K = 60
"""RRF damping constant -- the value from Cormack et al.'s original RRF paper and the de facto
default. Larger k flattens the contribution of top ranks (less winner-take-all)."""

DEFAULT_RETURN_PROPERTIES = ("name", "qualified_name", "doc", "source")


def rrf_fuse(ranked_id_lists: list[list[str]], k: int = RRF_K) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion over several best-first id lists -> (id, fused_score) pairs, highest
    first. score(d) = Σ 1/(k + rank_d) over the lists that contain d (rank is 1-based); a list
    without d contributes nothing. Deterministic and scale-free (uses ranks, not raw scores)."""
    scores: dict[str, float] = {}
    for ids in ranked_id_lists:
        for rank, node_id in enumerate(ids, 1):
            scores[node_id] = scores.get(node_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: -kv[1])


def hybrid_search(
    writer: GraphWriter,
    embedder: Embedder | None,
    query: str,
    k: int = 10,
    *,
    return_properties: tuple[str, ...] = DEFAULT_RETURN_PROPERTIES,
    candidates: int | None = None,
) -> list[dict]:
    """Fused lexical + vector search. Pulls `candidates` (default max(3k, 20)) from each ranker,
    fuses by RRF, returns the top `k` rows (`id`, `kind`, the projected props, and a fused
    `score`), best-first. `embedder=None` or a model that can't load -> lexical-only; a graph
    without a full-text index yet -> vector-only. Returns [] only when both rankers are empty."""
    n = candidates or max(3 * k, 20)
    lexical = writer.fulltext_search(query, n, return_properties=return_properties)
    vector: list[dict] = []
    if embedder is not None:
        try:
            vector = writer.knn_search(embedder.embed_query(query), n, return_properties=return_properties)
        except Exception:  # model unavailable (offline / not installed) -> lexical-only
            logger.debug("hybrid_search: vector ranker unavailable; lexical-only", exc_info=True)

    fused = rrf_fuse([[r["id"] for r in lexical], [r["id"] for r in vector]])

    # Merge the two rankers' rows so the fused result carries the node's properties regardless of
    # which ranker surfaced it (both project the same props; either row will do).
    rows_by_id: dict[str, dict] = {}
    for r in vector:
        rows_by_id[r["id"]] = r
    for r in lexical:
        rows_by_id.setdefault(r["id"], r)

    out: list[dict] = []
    for node_id, score in fused[:k]:
        row = dict(rows_by_id.get(node_id, {"id": node_id}))
        row["score"] = round(score, 6)  # fused RRF score (higher = better), replaces per-ranker score
        out.append(row)
    return out
