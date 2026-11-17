"""Pure diff logic between a graph's current live state and the full desired
state for some target -- no knowledge of git or any specific ingestor.

This is what makes moving a graph between any two states -- forward, backward, or
across branches -- the same operation, always correct by construction: rather than
hand-deriving graph edits from e.g. a source-level diff (which breaks for any
ingestor whose resolution is global rather than per-file -- see the C# ingestor's
git-sync design discussion), the target state is always computed fresh in full, and
diffed against whatever the graph currently holds. The "backward" direction is the
same function call with the two arguments swapped, not a separately-derived inverse.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field

from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.core.instrument import phase

NodeKey = tuple[str, tuple[tuple[str, object], ...]]
EdgeKey = tuple[str, str, tuple[tuple[str, object], ...], str, tuple[tuple[str, object], ...]]


@dataclass
class GraphDelta:
    nodes_to_upsert: list[GraphNode] = field(default_factory=list)
    nodes_to_remove: list[tuple[str, dict]] = field(default_factory=list)  # (label, key)
    edges_to_upsert: list[GraphEdge] = field(default_factory=list)
    edges_to_remove: list[tuple[str, str, dict, str, dict]] = field(default_factory=list)  # (rel_type, from_label, from_key, to_label, to_key)


def _sorted_items(mapping: dict) -> tuple[tuple[str, object], ...]:
    return tuple(sorted(mapping.items()))


def node_key(node: GraphNode) -> NodeKey:
    return (node.label, _sorted_items(node.key))


def edge_key(edge: GraphEdge) -> EdgeKey:
    return (edge.rel_type, edge.from_label, _sorted_items(edge.from_key), edge.to_label, _sorted_items(edge.to_key))


# The `_fingerprint` is a content-change detector, not a security hash, and is stored on every
# node and edge. 16 hex chars (a 64-bit SHA-256 prefix) is plenty: a collision would only miss a
# re-index of ONE entity whose old and new content happened to share the prefix (~1 in 1.8e19 per
# changed entity), recovered on its next change -- while cutting the stored fingerprint ~4x vs the
# full 64-char digest (node fingerprints are near-unique, so interning can't dedupe them).
# AURYN_FP_LEN overrides the length; keep it stable per graph (a length change makes stored !=
# freshly-computed, forcing one harmless full resync, not corruption).
_FP_LEN = max(8, min(64, int(os.getenv("AURYN_FP_LEN", "16"))))


def fingerprint(properties: dict) -> str:
    """Deterministic hash of a node/edge's non-key properties. GraphWriter stores
    this as `_fingerprint` alongside the real properties at write time; diffing
    then only has to fetch that short hash back (see GraphWriter.read_fingerprints)
    instead of the full properties blob -- which can include multi-KB `source`
    text -- to decide whether something changed. Computed in Python on both sides
    of the comparison (never re-derived from what FalkorDB actually persisted, so
    FalkorDB's own null-property-drops etc. can't cause a mismatch) -- deliberately
    not pushed into a Cypher/UDF hash function, since that would mean maintaining
    two implementations of the same algorithm that must never drift apart; a
    single Python implementation is the only thing that has to agree with itself.
    json.dumps(sort_keys=True) makes key order irrelevant; every property value
    this project ever produces is a plain str/int/bool/None/list, all JSON-safe."""
    return hashlib.sha256(json.dumps(properties, sort_keys=True).encode()).hexdigest()[:_FP_LEN]


def compute_delta(
    current_node_fingerprints: dict[NodeKey, str],
    current_edge_fingerprints: dict[EdgeKey, str],
    target: Iterable[GraphNode | GraphEdge],
) -> GraphDelta:
    """Instrumented entry point (see core/instrument.py): the diff itself lives in
    `_compute_delta`; this wrapper only times it under the "diff" phase when a
    recorder is active, and is otherwise a straight pass-through."""
    with phase("diff"):
        return _compute_delta(current_node_fingerprints, current_edge_fingerprints, target)


def _compute_delta(
    current_node_fingerprints: dict[NodeKey, str],
    current_edge_fingerprints: dict[EdgeKey, str],
    target: Iterable[GraphNode | GraphEdge],
) -> GraphDelta:
    """`current_node_fingerprints`/`current_edge_fingerprints` are the graph's live
    state right now, as key -> fingerprint (e.g. from GraphWriter.read_fingerprints)
    rather than full nodes/edges -- fetching a full properties blob just to find
    out whether it changed is wasteful when a short stored hash answers the same
    question. `target` is the complete, freshly-computed desired state. Anything
    in target that's new or whose fingerprint differs from its current counterpart
    is upserted; anything current with no counterpart in target is removed."""
    target_nodes: dict[NodeKey, GraphNode] = {}
    target_edges: dict[EdgeKey, GraphEdge] = {}
    for item in target:
        if isinstance(item, GraphNode):
            target_nodes[node_key(item)] = item
        else:
            target_edges[edge_key(item)] = item

    delta = GraphDelta()

    for key, node in target_nodes.items():
        if current_node_fingerprints.get(key) != fingerprint(node.properties):
            delta.nodes_to_upsert.append(node)
    for key in current_node_fingerprints.keys() - target_nodes.keys():
        label, key_items = key
        delta.nodes_to_remove.append((label, dict(key_items)))

    for key, edge in target_edges.items():
        if current_edge_fingerprints.get(key) != fingerprint(edge.properties):
            delta.edges_to_upsert.append(edge)
    for key in current_edge_fingerprints.keys() - target_edges.keys():
        rel_type, from_label, from_items, to_label, to_items = key
        delta.edges_to_remove.append((rel_type, from_label, dict(from_items), to_label, dict(to_items)))

    return delta


# -- Streaming apply (memory-bounded alternative to compute_delta + apply_delta) -----

_STREAM_BATCH_SIZE = 2000
"""Items buffered before a batched write. Matches GraphWriter's own _WRITE_BATCH_SIZE
(which re-chunks at the same size) so each flush is exactly one server round-trip; kept
as its own constant here to avoid graph_sync importing graph_client (which imports us)."""


@dataclass
class DeltaStats:
    """The add/update/remove tallies stream_apply() produces -- the same numbers a
    caller would otherwise derive from a GraphDelta, so each ingestor's SyncStats can be
    filled identically whether it uses compute_delta+apply_delta or stream_apply."""

    nodes_added: int = 0
    nodes_updated: int = 0
    nodes_removed: int = 0
    edges_added: int = 0
    edges_updated: int = 0
    edges_removed: int = 0


_MISSING = object()


SYNC_MARKER = "commit_sha"
"""The property a sync short-circuits on: every engine opens with "is the Project node already at
`target_sha`?" and returns at once if so. So while a sync is changing the graph the node carrying
it must claim NO commit: the stamp is cleared before the first mutation and written LAST. Stamped
first (it used to be the first item merged), a sync that died mid-apply left a half-written graph
that every later sync took for current; and merely keeping the OLD stamp until the end would let
a sync back to that commit short-circuit over the same half-written graph. With no stamp the next
sync -- to any commit -- recomputes in full against what the graph actually holds."""


def unstamped(node: GraphNode) -> GraphNode:
    """`node` with its SYNC_MARKER cleared (a None value removes the property): written before
    anything else changes -- it also brings a brand-new Project node into existence for its edges."""
    return GraphNode(node.label, node.key, {**node.properties, SYNC_MARKER: None})


def stream_apply(
    writer,
    id_prefix: str,
    node_labels: list[str],
    make_target: Callable[[], Iterator[GraphNode | GraphEdge]],
    *,
    diff_extern_edges: bool = True,
) -> tuple[DeltaStats, list[str], list[str]]:
    """Memory-bounded equivalent of `compute_delta(...)` followed by
    `writer.apply_delta(...)`, for the recompute-and-diff sync every file-based ingestor
    does. Rather than materialising the *entire* freshly-computed target graph as a list
    (and then again as dicts inside compute_delta), it streams the target and writes each
    changed node/edge in batches, so peak memory is O(current fingerprints + one batch)
    instead of O(whole target graph). On a first ingest -- where the live graph is empty,
    so the current path's target list *is* the whole graph -- this is the difference
    between holding every node/edge (with its multi-KB `source` text) at once and holding
    almost nothing.

    Two mechanics make it both correct and cheap:

    * Removals with no separate "seen" set: each matched target key is popped out of the
      current-fingerprint dict, so whatever remains once the stream is exhausted is
      exactly what the target no longer contains -- i.e. the removals -- while the dict we
      already had to read is the only per-key structure kept.
    * Nodes before edges, unconditionally: `make_target` is called TWICE (a nodes pass,
      then an edges pass), so every node is written before any edge can reference it,
      regardless of the order the target emits them in -- the same guarantee
      apply_delta() gives by writing all merge_nodes before all merge_edges, without
      buffering the edges. `make_target` must therefore return a FRESH, equivalent
      iterator each call (e.g. `lambda: chain(project_items, resolution.resolve(raws, id))`).

    Returns (stats, real_linkages, placeholder_ids) -- the linkage strings are collected
    from the node stream inline (see core/linkage.collect_linkages) so the caller can run
    reconcile_linkages() without a materialised list to walk.

    `diff_extern_edges` (default True): whether a `CALLS -> extern://` placeholder edge the live
    graph holds but this target doesn't re-emit is treated as a removal. True for a single, complete
    pass -- the fast pass of a two-phase language, and any single-phase full recompute -- whose target
    IS authoritative for placeholders, so a dropped unresolved call (a libc/macro/out-of-project
    symbol) correctly prunes its stale placeholder. The DEEP pass of a two-phase language sets it False:
    its engine (jedi/Roslyn) can't resolve cross-repo and so omits the placeholders the fast pass
    authored, and would otherwise diff them away before reconcile_linkages heals them (the cross-repo
    `consumer_first` heal). The placeholders are owned by the fast pass + the linkage reconciler, not
    the deep upgrade. Only extern:// targets are affected; in-prefix edges diff normally either way."""
    with phase("read_fingerprints"):
        node_fps, edge_fps = writer.read_fingerprints(id_prefix, node_labels)
    stats = DeltaStats()
    reals: list[str] = []
    placeholders: list[str] = []

    with phase("write"):
        # Pass 1 -- nodes. Pop each target node's key out of node_fps: hit -> compare
        # fingerprints (unchanged/updated), miss -> new. Leftover node_fps = removals.
        marker: GraphNode | None = None  # the node carrying SYNC_MARKER -- written last, see below
        batch: list[GraphNode] = []
        updated_pairs: list[tuple[str, str]] = []  # (label, id) of content-changed nodes
        for item in make_target():
            if not isinstance(item, GraphNode):
                continue
            props = item.properties
            if "linkage_key" in props:  # cross-repo linkage (see linkage.collect_linkages)
                if props.get("resolved") is False:
                    placeholders.append(str(item.key["id"]))
                else:
                    reals.append(str(props["linkage_key"]))
            prev = node_fps.pop(node_key(item), _MISSING)
            if prev is _MISSING:
                stats.nodes_added += 1
            elif prev != fingerprint(props):
                stats.nodes_updated += 1
                # Content changed -> its (worker-written) embedding is now stale; queue it to be
                # cleared so the embed worker recomputes. New nodes have none, so only updates.
                updated_pairs.append((item.label, str(item.key["id"])))
            else:
                continue  # unchanged -- already popped, so it won't be counted a removal
            if SYNC_MARKER in props:
                # Clear the stamp right now, before anything else is written, and hold the real
                # one back until everything is applied (see SYNC_MARKER). "Before anything else"
                # holds because every engine's target LEADS with its Project node: a node yielded
                # ahead of it could reach the graph in a batch while the old stamp still stood.
                marker = item
                writer.merge_nodes([unstamped(item)])
                continue
            batch.append(item)
            if len(batch) >= _STREAM_BATCH_SIZE:
                writer.merge_nodes(batch)
                batch.clear()
        if batch:
            writer.merge_nodes(batch)
        if updated_pairs:
            writer.invalidate_embeddings(updated_pairs)

        # Pass 2 -- edges, once every node above is guaranteed to exist.
        edge_batch: list[GraphEdge] = []
        for item in make_target():
            if not isinstance(item, GraphEdge):
                continue
            prev = edge_fps.pop(edge_key(item), _MISSING)
            if prev is _MISSING:
                stats.edges_added += 1
            elif prev != fingerprint(item.properties):
                stats.edges_updated += 1
            else:
                continue
            edge_batch.append(item)
            if len(edge_batch) >= _STREAM_BATCH_SIZE:
                writer.merge_edges(edge_batch)
                edge_batch.clear()
        if edge_batch:
            writer.merge_edges(edge_batch)

        # Removals: keys the live graph still holds that the target never re-emitted.
        # Edges before nodes (a node's DETACH would also drop edges we'd rather account
        # for explicitly) -- same order apply_delta() uses.
        if not diff_extern_edges:
            # A deep pass doesn't own `extern://` placeholder edges (see diff_extern_edges above) --
            # keep any it didn't re-emit, so reconcile_linkages still has them to heal.
            edge_fps = {k: v for k, v in edge_fps.items()
                        if not str(dict(k[4]).get("id", "")).startswith("extern://")}
        if edge_fps:
            writer.delete_edges(
                [(rt, fl, dict(fk), tl, dict(tk)) for (rt, fl, fk, tl, tk) in edge_fps]
            )
            stats.edges_removed = len(edge_fps)
            # A removed edge to an `extern://` cross-repo placeholder (ADR 0004) may have been its
            # LAST referrer. The placeholder lives outside this project's id prefix, so the node read
            # above never saw it -- node_fps can't carry it, and without this it lingers as a stray a
            # full recompute never emits (incremental != full; the authored engine reclaims the same
            # way). Bounded to the extern targets this diff actually dropped; each check is an id seek.
            extern_candidates = {
                (dict(tk)["id"], tl) for (rt, fl, fk, tl, tk) in edge_fps
                if str(dict(tk).get("id", "")).startswith("extern://")
            }
            stats.nodes_removed += _reclaim_orphan_placeholders(writer, extern_candidates)
        if node_fps:
            writer.delete_nodes([(label, dict(k)) for (label, k) in node_fps])
            stats.nodes_removed += len(node_fps)
        if marker is not None:
            writer.merge_nodes([marker])  # every node, edge and removal is in: stamp the commit

    return stats, reals, placeholders


def _reclaim_orphan_placeholders(writer, candidates: set[tuple[str, str]]) -> int:
    """Delete each `extern://` placeholder in `candidates` that now has zero inbound edges (its last
    referrer was removed this sync). The zero-inbound test and the delete are ONE query (`WHERE NOT
    (n)<--()  DELETE n`) so a concurrent background ingest can't add a referrer between a separate
    check and delete and have it dropped (TOCTOU). A referrer in ANY project (multi-repo) keeps it.
    `count` reflects nodes actually deleted. No scan -- one id-indexed seek per bounded candidate."""
    reclaimed = 0
    for eid, label in candidates:
        res = writer.query(
            f"MATCH (n:{label}) WHERE n.id = $id AND NOT (n)<--() DELETE n RETURN count(n)",
            {"id": eid},
        )
        rows = res.result_set
        if rows and rows[0][0]:
            reclaimed += int(rows[0][0])
    return reclaimed
