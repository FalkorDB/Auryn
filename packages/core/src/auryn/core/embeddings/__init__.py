"""Semantic (vector) search over the code graph.

Embeddings are stored as a property on the existing graph nodes and indexed by FalkorDB's
native vector index -- one store, hybrid graph+vector queries in one query language, no second
system to keep in sync. The text embedded for each node is assembled from its shared `doc` +
`source` properties (see `documents.py`); the embedder that turns that text into a vector is
pluggable (`embedder.py`), defaulting to a lean local ONNX model and optionally an external API.
"""

from __future__ import annotations

from auryn.core.embeddings.documents import build_document
from auryn.core.embeddings.search import hybrid_search, rrf_fuse
from auryn.core.embeddings.embedder import (
    DEFAULT_LOCAL_DIM,
    DEFAULT_LOCAL_MODEL,
    Embedder,
    FastEmbedEmbedder,
    configured_model,
    embeddings_enabled,
    get_embedder,
    known_dim,
)

__all__ = [
    "build_document",
    "hybrid_search",
    "rrf_fuse",
    "Embedder",
    "FastEmbedEmbedder",
    "get_embedder",
    "known_dim",
    "embeddings_enabled",
    "configured_model",
    "DEFAULT_LOCAL_MODEL",
    "DEFAULT_LOCAL_DIM",
]
