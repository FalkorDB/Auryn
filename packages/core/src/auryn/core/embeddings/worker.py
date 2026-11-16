"""Compute embeddings for the graph's callables and named types, then index them.

`embed_graph` scans for embeddable, source-bearing nodes that don't yet have an `embedding`,
builds each one's document (`documents.build_document`), embeds them in batches with the
configured `Embedder`, and writes the vectors. It then ensures the vector index exists:

- **First run** -> every vector is written and the index is created once at the end.
  Compute-then-index is far cheaper than maintaining an HNSW index through a bulk load.
- **Later runs** -> the index already exists (create is a no-op), so newly written / re-embedded
  nodes are indexed as they're written.

The work queue is implicit: a node needs embedding iff it has content (`source`/`doc`, or -- for
a container -- child symbols) but no `embedding` property. An incremental resync that changes a
node's content clears its embedding (`GraphWriter.invalidate_embeddings`), so it re-appears in the
queue -- no per-node model/dim/hash bookkeeping (see `embedder.py`).

What gets embedded:
- **Callables** (Function/Method) -- `doc` + `source`, directly from the node.
- **Named types** (Class/Struct/Interface/...) -- `doc` + declaration header, enriched with member
  signatures gathered from the graph (HAS_METHOD/HAS_FIELD/HAS_PROPERTY children).
- **Containers** (File/Module/Package/Namespace) -- a synthesized symbol inventory (the names of
  the callables/types they contain, via outgoing HAS_* / incoming DEFINED_IN). Best-effort and
  language-shape-dependent: a container is embedded only when that traversal yields symbols (or it
  has a doc), so bare/empty containers are skipped.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass

from auryn.core.embeddings.documents import (
    CALLABLE_LABELS,
    CONTAINER_LABELS,
    TYPE_LABELS,
    build_document,
)
from auryn.core.embeddings.embedder import Embedder, get_embedder
from auryn.core.graph_client import GraphWriter

logger = logging.getLogger(__name__)

# Nodes that store `source`/`doc` directly. Sorted for deterministic scan order.
_SOURCE_BEARING_LABELS = tuple(sorted(CALLABLE_LABELS | TYPE_LABELS))
_CONTAINER_SCAN_LABELS = tuple(sorted(CONTAINER_LABELS))
# Child labels a container's symbol inventory pulls in (callables + named types).
_MEMBER_KINDS = sorted(CALLABLE_LABELS | TYPE_LABELS)

_DEFAULT_BATCH = 128
"""Documents embedded per model call. Batching amortizes model-call overhead; kept modest so a
single batch's texts (which can each be a full function body) stay a reasonable payload."""

_ID_CHUNK = 1000
"""Ids per `WHERE id IN $ids` member-gathering query -- keeps a single request's parameter list
bounded regardless of how many types/containers are pending."""


@dataclass
class EmbedStats:
    scanned: int = 0  # nodes found pending (had content, no embedding)
    embedded: int = 0  # vectors actually written (scanned minus any with an empty document)
    model_id: str = ""
    dim: int = 0
    index_ready: bool = False

    def to_dict(self) -> dict:
        return {
            "scanned": self.scanned,
            "embedded": self.embedded,
            "model_id": self.model_id,
            "dim": self.dim,
            "index_ready": self.index_ready,
        }


def _chunked(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _scan_pending(writer: GraphWriter, labels: tuple[str, ...]) -> list[tuple[str, str, dict]]:
    """(kind_label, node_id, props) for every embeddable node with content but no embedding yet.
    One index-backed query per label; only the properties the document assembler needs are
    fetched (not full nodes)."""
    pending: list[tuple[str, str, dict]] = []
    for label in labels:
        query = (
            f"MATCH (n:{label}) "
            "WHERE n.embedding IS NULL AND (coalesce(n.source, '') <> '' OR coalesce(n.doc, '') <> '') "
            "RETURN n.id, n.name, n.qualified_name, n.doc, n.source"
        )
        for node_id, name, qname, doc, source in writer.ro_query(query).result_set:
            pending.append(
                (label, node_id, {"name": name, "qualified_name": qname, "doc": doc, "source": source})
            )
    return pending


def _scan_pending_containers(writer: GraphWriter, labels: tuple[str, ...]) -> list[tuple[str, str, dict]]:
    """(kind_label, node_id, props) for every container node (File/Module/Package/...) without an
    embedding yet. Containers store no `source`, so the filter is just embedding-absence; whether
    a container is worth embedding is decided later, once its symbol inventory is gathered."""
    pending: list[tuple[str, str, dict]] = []
    for label in labels:
        query = (
            f"MATCH (c:{label}) WHERE c.embedding IS NULL "
            "RETURN c.id, c.name, c.qualified_name, c.doc"
        )
        for node_id, name, qname, doc in writer.ro_query(query).result_set:
            pending.append((label, node_id, {"name": name, "qualified_name": qname, "doc": doc, "source": ""}))
    return pending


def _gather_type_members(writer: GraphWriter, pending: list[tuple[str, str, dict]]) -> dict[str, list[str]]:
    """Member signatures per type id, for enriching a type's document beyond its declaration
    header: the `signature` (falling back to `name`) of each HAS_METHOD/HAS_FIELD/HAS_PROPERTY
    child. Batched by label + id chunk so it's index-backed, not a scan."""
    ids_by_label: dict[str, list[str]] = defaultdict(list)
    for label, node_id, _ in pending:
        if label in TYPE_LABELS:
            ids_by_label[label].append(node_id)
    out: dict[str, list[str]] = {}
    for label, ids in ids_by_label.items():
        for chunk in _chunked(ids, _ID_CHUNK):
            rows = writer.ro_query(
                f"MATCH (t:{label})-[:HAS_METHOD|HAS_FIELD|HAS_PROPERTY]->(m) WHERE t.id IN $ids "
                "RETURN t.id, collect(coalesce(m.signature, m.name))",
                {"ids": chunk},
            ).result_set
            for tid, sigs in rows:
                vals = [s for s in sigs if s]
                if vals:
                    out[tid] = vals
    return out


def _gather_container_members(writer: GraphWriter, containers: list[tuple[str, str, dict]]) -> dict[str, list[str]]:
    """Symbol inventory per container id: the names of the callables/types it contains -- via any
    outgoing edge to a callable/type-labeled node (HAS_FUNCTION/HAS_CLASS/HAS_TYPE/... across
    languages) and via incoming DEFINED_IN (types/functions that declare themselves in a File).
    `any(l IN labels(x) ...)` (not `labels(x)[0]`) so an already-embedded child -- which also
    carries the shared marker label -- is still recognised."""
    ids_by_label: dict[str, list[str]] = defaultdict(list)
    for label, node_id, _ in containers:
        ids_by_label[label].append(node_id)
    out: dict[str, list[str]] = defaultdict(list)
    for label, ids in ids_by_label.items():
        for chunk in _chunked(ids, _ID_CHUNK):
            for cid, names in writer.ro_query(
                f"MATCH (c:{label})-->(x) WHERE c.id IN $ids AND x.name IS NOT NULL "
                "AND any(l IN labels(x) WHERE l IN $kinds) "
                "RETURN c.id, collect(DISTINCT x.name)",
                {"ids": chunk, "kinds": _MEMBER_KINDS},
            ).result_set:
                out[cid].extend(n for n in names if n)
            for cid, names in writer.ro_query(
                f"MATCH (x)-[:DEFINED_IN]->(c:{label}) WHERE c.id IN $ids AND x.name IS NOT NULL "
                "RETURN c.id, collect(DISTINCT x.name)",
                {"ids": chunk},
            ).result_set:
                out[cid].extend(n for n in names if n)
    return {cid: list(dict.fromkeys(names)) for cid, names in out.items()}  # dedup, keep order


def embed_graph(
    writer: GraphWriter,
    embedder: Embedder | None = None,
    *,
    batch_size: int = _DEFAULT_BATCH,
) -> EmbedStats:
    """Embed every pending callable/type/container node and (re)ensure the vector index.
    Idempotent: a second run with no new/changed content embeds nothing. Returns EmbedStats."""
    embedder = embedder or get_embedder()
    pending = _scan_pending(writer, _SOURCE_BEARING_LABELS)
    containers = _scan_pending_containers(writer, _CONTAINER_SCAN_LABELS)

    # Enrich types (member signatures) and containers (symbol inventory) with graph context.
    members = _gather_type_members(writer, pending)
    members.update(_gather_container_members(writer, containers))

    documents: list[tuple[str, str, str]] = []  # (kind_label, node_id, document text)
    for kind_label, node_id, props in pending:
        document = build_document(kind_label, props, members=members.get(node_id))
        if document:
            documents.append((kind_label, node_id, document))
    for kind_label, node_id, props in containers:
        mem = members.get(node_id)
        if not mem and not (props.get("doc") or "").strip():
            continue  # bare container (no inventory, no doc) -- nothing worth embedding
        document = build_document(kind_label, props, members=mem)
        if document:
            documents.append((kind_label, node_id, document))

    scanned = len(pending) + len(containers)
    logger.info(
        "Embedding %d node(s) (%d scanned) with %s (dim=%d)",
        len(documents), scanned, embedder.model_id, embedder.dim,
    )

    embedded = 0
    for chunk in _chunked(documents, batch_size):
        vectors = embedder.embed([doc for _, _, doc in chunk])
        writer.write_embeddings(
            [(kind_label, node_id, vec) for (kind_label, node_id, _), vec in zip(chunk, vectors)]
        )
        embedded += len(chunk)
        logger.debug("Embedded %d/%d", embedded, len(documents))

    # Compute-then-index: create the vector index after the vectors are written (first run) or
    # confirm it exists (later runs -- create is a no-op). Only when something was written, so a
    # no-op run doesn't touch the schema.
    index_ready = False
    if embedded:
        writer.create_vector_index(
            writer.SHARED_EMBEDDING_LABEL, writer.EMBEDDING_PROPERTY, embedder.dim
        )
        # The lexical half of hybrid retrieval: a full-text index over name+doc on the same
        # shared label, so `search_code` can fuse keyword + semantic ranking (see embeddings/search).
        writer.create_fulltext_index(writer.SHARED_EMBEDDING_LABEL, *writer.FULLTEXT_PROPERTIES)
        # CREATE INDEX returns before the index is built, and a search against one still under
        # construction comes back empty -- so "ready" waits until FalkorDB says it is.
        index_ready = writer.await_indexes(writer.SHARED_EMBEDDING_LABEL)

    stats = EmbedStats(
        scanned=scanned,
        embedded=embedded,
        model_id=embedder.model_id,
        dim=embedder.dim,
        index_ready=index_ready,
    )
    logger.info("Embedding complete: %s", stats.to_dict())
    return stats


def reembed_all(writer: GraphWriter, embedder: Embedder | None = None, *, batch_size: int = _DEFAULT_BATCH) -> EmbedStats:
    """Drop the vector index and every stored vector, then embed the whole graph from scratch.
    This is the model-switch path: the index is fixed-dimension, so a new model needs a fresh
    index (see embedder.py). Order matters -- drop the old (possibly different-dimension) index
    BEFORE embed_graph creates the new one, or create_vector_index would no-op onto the stale
    index."""
    embedder = embedder or get_embedder()
    logger.info("Re-embedding whole graph with %s (dim=%d): dropping index + clearing vectors",
                embedder.model_id, embedder.dim)
    writer.drop_vector_index(writer.SHARED_EMBEDDING_LABEL, writer.EMBEDDING_PROPERTY)
    writer.clear_all_embeddings()
    return embed_graph(writer, embedder, batch_size=batch_size)
