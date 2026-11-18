"""Read-only Cypher queries against a FalkorDB Graph, plus Node -> dict serialization.

Nothing here hardcodes a label or relationship type -- these queries work
unmodified regardless of which ingestor(s) wrote the graph. Every query goes
through Graph.ro_query(), never .query(), so nothing built on this module is
structurally capable of mutating a graph no matter what a caller passes in.

Shared by the viewer (src/auryn/viewer/app.py) and the MCP server
(src/auryn/mcp/server.py) -- one query layer, not two independently
maintained copies of "how to serialize a node" or "what counts as read-only."
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from falkordb import FalkorDB, Graph
from falkordb.edge import Edge
from falkordb.node import Node

try:  # Path may be absent in older client versions; degrade gracefully
    from falkordb.path import Path
except Exception:  # pragma: no cover - defensive
    Path = None

from auryn.core.graph_client import SHARED_NODE_LABEL, first_semantic_label, semantic_label

DEFAULT_ROOT_LIMIT = 200
DEFAULT_SEARCH_LIMIT = 50
DEFAULT_ALL_LIMIT = 1000
DEFAULT_TRAVERSAL_LIMIT = 200
DEFAULT_MAX_DEPTH = 3
MAX_ALLOWED_DEPTH = 10
# FalkorDB's own default (GRAPH.CONFIG TIMEOUT, 1000ms) is tuned for small
# interactive queries and is too tight for the full-graph scans below on a
# large real codebase -- confirmed empirically: root_nodes/overview/search_nodes
# all timed out against a real ~188k-node/423k-edge ingested graph under the
# server default. These are still legitimate, bounded (LIMIT-capped) browsing
# operations, not runaway queries, so they get a more generous explicit budget
# rather than silently inheriting a timeout tuned for a different workload.
DEFAULT_QUERY_TIMEOUT_MS = 15000
# CALLS/DEPENDS_ON/READS/WRITES/REFERENCES are the edges every ingestor already
# treats as "this uses that" -- EXTENDS/IMPLEMENTS/HAS_* are structural, not
# usage, and are intentionally left out of the default so impact_of doesn't
# report "everything in the same namespace" as impacted by default. INVOKES is the
# derived cross-service call the HTTP stitch writes (caller -> handler, across the
# request/response seam) so blast radius and reachability span repos and languages
# in one traversal instead of dying at the first HTTP boundary.
DEFAULT_IMPACT_EDGE_TYPES = ("CALLS", "INVOKES", "DEPENDS_ON", "READS", "WRITES", "REFERENCES")
DEFAULT_CLOSURE_CALL_EDGE_TYPES = ("CALLS", "INVOKES")
DEFAULT_CLOSURE_DATA_EDGE_TYPES = ("READS", "WRITES")

_SAFE_REL_TYPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _is_internal_key(key) -> bool:
    """A property key never surfaced to a caller:
    * underscore-prefixed bookkeeping (e.g. `_fingerprint`, the content hash git_sync uses to
      detect changes -- see graph_sync.fingerprint());
    * the `embedding` vector (a ~768-float array the embeddings worker writes for semantic
      search -- see core/embeddings; used only by the KNN index, never returned as an attribute
      and not advertised as a queryable key in the schema).

    Single source of truth for both `_visible_properties` (strips it off returned entities) and
    `graph_schema` (omits it from the advertised property-key list)."""
    key = str(key)
    return key.startswith("_") or key == "embedding"


def _visible_properties(props: dict) -> dict:
    """The properties of a node/edge with internal keys removed (see `_is_internal_key`).

    Every node/edge returned to a caller routes through here (serialize_node,
    serialize_node_lean, raw run_cypher, and edge serialization), so this is the single point
    that guarantees the embedding vector never rides along on a returned entity. Semantic search
    itself selects only the columns it needs (GraphWriter.knn_search return_properties), so it
    never fetches the vector in the first place."""
    return {k: v for k, v in props.items() if not _is_internal_key(k)}


def serialize_node(node: Node) -> dict:
    label = first_semantic_label(node.labels)  # strip system labels (Entity/Embeddable) -- caller sees File/Function/...
    props = _visible_properties(node.properties)
    return {
        "id": props.get("id"),
        "label": label,
        "name": props.get("name") or props.get("id"),
        "properties": props,
    }


def _lean_props(props: dict) -> dict:
    """Identifying/locating properties only -- drops the `source` text and any bulky value
    (long strings, big lists). Keeps a traversal result compact so a list of dozens of nodes
    doesn't flood the agent's context; the agent can get_context(id) for the full node."""
    out = {}
    for k, v in props.items():
        if k.startswith("_") or k == "source":
            continue
        if isinstance(v, str) and len(v) > 200:
            continue
        if isinstance(v, (list, dict)) and len(str(v)) > 200:
            continue
        out[k] = v
    return out


def serialize_node_lean(node: Node) -> dict:
    """serialize_node, but with only compact identifying/locating properties -- for the
    list-returning traversal tools (search/who_calls/callees/impact/closure/path) where the
    full property bag * N nodes would dominate the response's token cost."""
    label = first_semantic_label(node.labels)  # strip system labels -- caller sees the dedicated label
    props = _visible_properties(node.properties)
    return {
        "id": props.get("id"),
        "label": label,
        "name": props.get("name") or props.get("id"),
        "properties": _lean_props(props),
    }


# Keys dropped from a traversal-LIST node's property bag (who_calls/callees/impact/trace/path):
# `id`/`name` are already top-level (pure duplication across dozens of nodes); `linkage_key` and
# `qualified_name` are internal / redundant with the id; the modifier booleans, return-type details
# and (usually empty) decorators/doc are dead weight the agent rarely needs in a *list*. A denylist
# (not an allowlist) so an ingestor's location key -- file/path/line_* under any name -- always
# survives. get_context(id) still returns the full node when the detail is actually wanted.
_MIN_NODE_DROP_KEYS = frozenset({
    "id", "name", "linkage_key", "qualified_name", "decorators", "unresolved_calls", "doc",
    "return_type", "return_type_source", "is_async", "is_generator", "is_static", "is_abstract",
    "is_virtual", "is_override", "accessibility", "kind",
})


def serialize_node_min(node: Node) -> dict:
    """serialize_node_lean, trimmed further for the traversal-LIST tools: keeps the top-level
    id/name/label plus only the locating/disambiguating props (signature, file/path, line_*),
    dropping the id/name duplication and the internal/low-value keys (_MIN_NODE_DROP_KEYS). On a
    who_calls/impact result of dozens of nodes this is the dominant token saving; the full bag is
    one get_context(id) away."""
    label = first_semantic_label(node.labels)  # strip system labels -- caller sees the dedicated label
    props = _visible_properties(node.properties)
    kept = {
        k: v for k, v in _lean_props(props).items()
        if k not in _MIN_NODE_DROP_KEYS and v not in ("", [], {}, None)
    }
    return {
        "id": props.get("id"),
        "label": label,
        "name": props.get("name") or props.get("id"),
        "properties": kept,
    }


def serialize_edge(from_id: str, to_id: str, rel_type: str, rel_props) -> dict:
    """One edge in the canonical shape every edge-returning tool uses: explicit **direction**
    (`from` -> `to`), the relation `type`, and -- lifted to the top level so a caller never has to
    dig through properties -- the `resolution` provenance (resolved / inferred / external, ADR 0004)
    that rides in the edge props. Direction is structural, so the reader never has to guess which
    way a relationship points; a formatter can render it as `from --type[resolution]--> to`. The
    full (internal-key-stripped) properties stay under `properties` for anything else."""
    props = _visible_properties(dict(rel_props))
    edge = {"from": from_id, "to": to_id, "type": rel_type, "properties": props}
    if "resolution" in props:
        edge["resolution"] = props["resolution"]
    return edge


def serialize_edge_lean(from_id: str, to_id: str, rel_type: str, rel_props) -> dict:
    """serialize_edge trimmed to the fields that carry meaning for a call-chain list --
    direction (`from` -> `to`), the relation `type`, and the `resolution`/`via` provenance
    when present. Drops the full property bag (`argument_types`, `type_arguments`, ...) that a
    who_calls/callees result would otherwise multiply across dozens of edges. The viewer's
    edge-returning paths (neighbors / all_nodes_and_edges / shortest_path) keep the full
    serialize_edge; only the traversal-list tools use this."""
    props = _visible_properties(dict(rel_props))
    edge = {"from": from_id, "to": to_id, "type": rel_type}
    if "resolution" in props:
        edge["resolution"] = props["resolution"]
    if "via" in props:
        edge["via"] = props["via"]
    return edge


def _label_counts(graph: Graph) -> list[tuple[str, int]]:
    rows = graph.ro_query(
        f"MATCH (n) RETURN {semantic_label('n')} AS label, count(n) AS count",
        timeout=DEFAULT_QUERY_TIMEOUT_MS,
    ).result_set
    return [(row[0], row[1]) for row in rows]


def overview(graph: Graph) -> dict:
    node_counts = _label_counts(graph)
    edge_rows = graph.ro_query(
        "MATCH ()-[r]->() RETURN type(r) AS rel_type, count(r) AS count",
        timeout=DEFAULT_QUERY_TIMEOUT_MS,
    ).result_set
    return {
        "nodes": [{"label": label, "count": count} for label, count in node_counts],
        "edges": [{"type": row[0], "count": row[1]} for row in edge_rows],
    }


def root_nodes(graph: Graph, limit: int = DEFAULT_ROOT_LIMIT) -> list[dict]:
    """Nodes with no incoming edge -- the natural entry points into the graph
    (Solution/Project/Database, typically). FalkorDB's Cypher dialect supports
    neither an inline pattern predicate (`WHERE NOT ()-->(n)`) nor an EXISTS
    subquery/function for this (both confirmed empirically to error) -- a
    per-node OPTIONAL MATCH + count() is the only expressible form, and run
    against the whole graph at once that's an O(nodes + edges) join that's fast
    on a small graph but can exceed FalkorDB's default query timeout on a large
    one (confirmed: this alone timed out against a real ~188k-node/423k-edge
    ingested codebase).

    Real root nodes are near-universally instances of a handful of small,
    low-cardinality "container" labels, so this scopes the same check to one
    label at a time via the cheap per-label counts _label_counts() already
    gets for overview(), smallest label first, stopping as soon as `limit` is
    filled -- typically resolving in milliseconds against the small labels
    without the huge ones (Method/Parameter/...) ever being touched. Falls
    through to checking every remaining label, largest last, only if `limit`
    isn't already filled, so correctness never depends on the size heuristic
    being right, only its speed in the common case."""
    labels_smallest_first = [
        label
        for label, _count in sorted(_label_counts(graph), key=lambda item: item[1])
        if label and _SAFE_REL_TYPE.match(label)
    ]

    roots: list[dict] = []
    for label in labels_smallest_first:
        remaining = limit - len(roots)
        if remaining <= 0:
            break
        rows = graph.ro_query(
            f"""
            MATCH (n:{label})
            OPTIONAL MATCH (m)-[r]->(n)
            WITH n, count(r) AS incoming
            WHERE incoming = 0
            RETURN n
            LIMIT $limit
            """,
            {"limit": remaining},
            timeout=DEFAULT_QUERY_TIMEOUT_MS,
        ).result_set
        roots.extend(serialize_node(row[0]) for row in rows)

    return roots


def search_nodes(
    graph: Graph, label: str | None, q: str | None, limit: int = DEFAULT_SEARCH_LIMIT
) -> list[dict]:
    rows = graph.ro_query(
        """
        MATCH (n)
        WHERE ($label IS NULL OR $label IN labels(n))
          AND ($q IS NULL OR (n.name IS NOT NULL AND toLower(n.name) CONTAINS toLower($q)))
        RETURN n
        LIMIT $limit
        """,
        {"label": label, "q": q, "limit": limit},
        timeout=DEFAULT_QUERY_TIMEOUT_MS,
    ).result_set
    return [serialize_node_lean(row[0]) for row in rows]


def all_nodes_and_edges(graph: Graph, limit: int = DEFAULT_ALL_LIMIT) -> dict:
    """Every node and edge in the graph, for "Expand All" -- capped at `limit`
    nodes so a huge graph can't hang the browser's layout; `truncated` tells the
    caller when that happened, rather than silently showing a partial graph as if
    it were the whole thing. Edges are only included if both endpoints made the
    cut -- vis-network errors on an edge pointing at a node it was never given.
    That filter runs inside the Cypher query (against the already-capped
    `node_ids`), not in Python after fetching every edge in the graph -- on a
    large graph, unconditionally pulling every edge (with full properties) over
    the wire just to discard most of them in Python was itself a real cost,
    independent of the query-timeout issue `root_nodes` above has."""
    node_rows = graph.ro_query(
        "MATCH (n) RETURN n LIMIT $limit", {"limit": limit + 1}, timeout=DEFAULT_QUERY_TIMEOUT_MS
    ).result_set
    truncated = len(node_rows) > limit
    nodes = [serialize_node(row[0]) for row in node_rows[:limit]]
    node_ids = [n["id"] for n in nodes]

    edge_rows = graph.ro_query(
        """
        MATCH (a:Entity)-[r]->(b:Entity)
        WHERE a.id IN $ids AND b.id IN $ids
        RETURN type(r), properties(r), a.id, b.id
        """,
        {"ids": node_ids},
        timeout=DEFAULT_QUERY_TIMEOUT_MS,
    ).result_set
    edges = [
        {"from": from_id, "to": to_id, "type": rel_type, "properties": _visible_properties(dict(props))}
        for rel_type, props, from_id, to_id in edge_rows
    ]
    return {"nodes": nodes, "edges": edges, "truncated": truncated}


def _collect_graph_objects(cell, nodes_by_internal: dict, raw_edges: list) -> None:
    """Recursively pull Node/Edge objects out of one result-set cell. A Cypher
    RETURN can hand back nodes, relationships, paths, lists of those, or plain
    scalars -- we keep the graph objects (keyed by FalkorDB's internal integer
    id) and ignore scalars, which have no place on the canvas."""
    if isinstance(cell, Node):
        nodes_by_internal[cell.id] = cell
    elif isinstance(cell, Edge):
        raw_edges.append(cell)
    elif Path is not None and isinstance(cell, Path):
        for n in cell.nodes():
            nodes_by_internal[n.id] = n
        for e in cell.edges():
            raw_edges.append(e)
    elif isinstance(cell, (list, tuple)):
        for inner in cell:
            _collect_graph_objects(inner, nodes_by_internal, raw_edges)


def _serialize_cell(cell):
    """JSON-friendly view of one result-set cell so a query can return data that ISN'T a graph
    entity -- counts, aggregations, property projections (`RETURN e.route, count(*)`). Graph
    objects are compacted (Node -> lean node, Edge -> type + endpoints, Path -> nodes/edges);
    lists and maps recurse; scalars (str/int/float/bool/None) pass through unchanged."""
    if isinstance(cell, Node):
        return serialize_node_lean(cell)
    if isinstance(cell, Edge):
        return {"type": cell.relation, "src": cell.src_node, "dest": cell.dest_node,
                "properties": _visible_properties(dict(cell.properties))}
    if Path is not None and isinstance(cell, Path):
        return {"nodes": [serialize_node_lean(n) for n in cell.nodes()],
                "edges": [e.relation for e in cell.edges()]}
    if isinstance(cell, (list, tuple)):
        return [_serialize_cell(c) for c in cell]
    if isinstance(cell, dict):
        return {k: _serialize_cell(v) for k, v in cell.items()}
    return cell  # scalar


def _result_columns(result) -> list[str]:
    """Column names from a FalkorDB result header. Header entries are typically (type, name)
    pairs; be defensive about bare names and bytes."""
    columns = []
    for col in getattr(result, "header", None) or []:
        name = col[-1] if isinstance(col, (list, tuple)) else col
        if isinstance(name, bytes):
            name = name.decode("utf-8", "replace")
        columns.append(str(name))
    return columns


def run_read_query(graph: Graph, cypher: str, limit: int = DEFAULT_ALL_LIMIT) -> dict:
    """Run an arbitrary user-supplied Cypher query **read-only** (GRAPH.RO_QUERY,
    so any write clause is rejected by FalkorDB itself, not by us) and reshape the
    result two ways: the {nodes, edges} the viewer's canvas speaks, AND `columns`/`rows`
    -- the raw tabular result set -- so a query that returns scalars/aggregations rather
    than graph entities (`RETURN e.route, count(*)`) isn't silently dropped.

    Nodes and relationships are harvested from every cell of the result set for the graph
    view. Relationships carry only their endpoints' *internal* ids, so any endpoint the
    query didn't also return as a node is resolved in one follow-up lookup by internal id --
    that way `MATCH ()-[r]->() RETURN r` still draws real edges instead of dropping them.
    `rows` mirrors the result set row-for-row (each row keyed by column name when the header
    is available, else a positional list), with graph objects compacted and scalars passed
    through. `node_count`/`edge_count`/`row_count` are pre-cap; `truncated` says whether the
    cap trimmed either the node list or the rows."""
    result = graph.ro_query(cypher, timeout=DEFAULT_QUERY_TIMEOUT_MS)

    nodes_by_internal: dict = {}
    raw_edges: list = []
    for row in result.result_set:
        for cell in row:
            _collect_graph_objects(cell, nodes_by_internal, raw_edges)

    # resolve edge endpoints that weren't themselves returned as nodes
    missing = {
        internal
        for e in raw_edges
        for internal in (e.src_node, e.dest_node)
        if internal not in nodes_by_internal
    }
    if missing:
        rows = graph.ro_query(
            "MATCH (n) WHERE id(n) IN $ids RETURN n",
            {"ids": list(missing)},
            timeout=DEFAULT_QUERY_TIMEOUT_MS,
        ).result_set
        for (node,) in rows:
            nodes_by_internal[node.id] = node

    serialized = [serialize_node(n) for n in nodes_by_internal.values()]
    # a node the viewer can render must have an "id" property to key on
    renderable = [n for n in serialized if n["id"] is not None]
    id_by_internal = {
        internal: node.properties.get("id") for internal, node in nodes_by_internal.items()
    }

    edges = []
    seen_edges = set()
    for e in raw_edges:
        from_id = id_by_internal.get(e.src_node)
        to_id = id_by_internal.get(e.dest_node)
        if from_id is None or to_id is None:
            continue
        key = (from_id, e.relation, to_id)
        if key in seen_edges:
            continue
        seen_edges.add(key)
        edges.append(
            {
                "from": from_id,
                "to": to_id,
                "type": e.relation,
                "properties": _visible_properties(dict(e.properties)),
            }
        )

    # Tabular view: the raw result set, so non-graph data (counts, aggregations, projected
    # properties) survives. Keyed by column name when the header lines up, else positional.
    columns = _result_columns(result)
    tabular_rows = []
    for row in result.result_set[:limit]:
        cells = [_serialize_cell(c) for c in row]
        if columns and len(columns) == len(cells):
            tabular_rows.append(dict(zip(columns, cells)))
        else:
            tabular_rows.append(cells)

    truncated = len(renderable) > limit or len(result.result_set) > limit
    return {
        "nodes": renderable[:limit],
        "edges": edges,
        "columns": columns,
        "rows": tabular_rows,
        "node_count": len(renderable),
        "edge_count": len(edges),
        "row_count": len(result.result_set),
        "truncated": truncated,
    }


def node_detail(graph: Graph, node_id: str) -> dict | None:
    rows = graph.ro_query(
        "MATCH (n:Entity {id: $id}) RETURN n", {"id": node_id}, timeout=DEFAULT_QUERY_TIMEOUT_MS
    ).result_set
    if not rows:
        return None
    return serialize_node(rows[0][0])


def node_summary(graph: Graph, node_id: str) -> dict | None:
    """Like node_detail, but returns the LEAN node (no `source` text, no bulky props) -- for the
    `center`/endpoint echo of the traversal tools (who_calls/callees/impact_of/trace_entrypoint/
    find_path). The caller already supplied this node's id and does NOT need its full source
    echoed back on every call; that echo, repeated across a multi-call investigation, was the
    single largest avoidable cost in those responses. get_context(id) still returns the full
    node with source when it's actually wanted."""
    rows = graph.ro_query(
        "MATCH (n:Entity {id: $id}) RETURN n", {"id": node_id}, timeout=DEFAULT_QUERY_TIMEOUT_MS
    ).result_set
    if not rows:
        return None
    return serialize_node_lean(rows[0][0])


def git_tracked_projects(graph: Graph) -> list[dict]:
    """Every Project node in this graph that git_sync.sync() created (i.e. has a
    repo_uri) -- a graph can hold several (different git-tracked codebases, plus
    whatever the SQL/PL/SQL ingestors wrote), so this always returns a list, never
    assumes there's exactly one."""
    rows = graph.ro_query("MATCH (p:Project) WHERE p.repo_uri IS NOT NULL RETURN p").result_set
    return [serialize_node(row[0]) for row in rows]


def repositories(graph: Graph) -> list[dict]:
    """Compact summary of the git-tracked repositories a graph REPRESENTS -- one entry per
    Project. A graph can hold a single repo or several indexed together (so cross-repo calls
    resolve into one graph), so this is always a list. Just enough per repo to recognize and
    route it: `name`, its `repo_uri`, the ingest `depth` ("fast"|"deep"), and the
    `base_commit` it's currently synced to. Cheaper than git_tracked_projects (which returns
    the full Project nodes) -- meant for the cross-graph landscape in graphs_overview()."""
    rows = graph.ro_query(
        "MATCH (p:Project) WHERE p.repo_uri IS NOT NULL "
        "RETURN p.name, p.repo_uri, p.depth, p.base_commit ORDER BY p.name"
    ).result_set
    return [{"name": r[0], "repo_uri": r[1], "depth": r[2], "base_commit": r[3]} for r in rows]


def neighbors(graph: Graph, node_id: str, limit: int = DEFAULT_ROOT_LIMIT) -> dict | None:
    """One node plus its immediate (1-hop) neighbors, each direction bounded
    independently at `limit`. Unbounded until now -- a node with a huge fan-out
    (e.g. a Project with thousands of HAS_FILE edges into a large ingested
    codebase) would otherwise return every single one of them: both slow
    (confirmed: 2s+ for ~195k edges against a real-shaped large graph, itself
    at risk of the same query-timeout problem root_nodes had) and overwhelming
    for the viewer to render in one go. `truncated` says so if either
    direction hit it, same convention as all_nodes_and_edges above."""
    center = node_detail(graph, node_id)
    if center is None:
        return None

    nodes_by_id: dict[str, dict] = {}
    edges: list[dict] = []
    truncated = False

    outgoing = graph.ro_query(
        "MATCH (n:Entity {id: $id})-[r]->(m) RETURN type(r), properties(r), m LIMIT $limit",
        {"id": node_id, "limit": limit + 1},
        timeout=DEFAULT_QUERY_TIMEOUT_MS,
    ).result_set
    if len(outgoing) > limit:
        truncated = True
        outgoing = outgoing[:limit]
    for rel_type, rel_props, neighbor in outgoing:
        serialized = serialize_node(neighbor)
        nodes_by_id[serialized["id"]] = serialized
        edges.append(serialize_edge(node_id, serialized["id"], rel_type, rel_props))

    incoming = graph.ro_query(
        "MATCH (n:Entity {id: $id})<-[r]-(m) RETURN type(r), properties(r), m LIMIT $limit",
        {"id": node_id, "limit": limit + 1},
        timeout=DEFAULT_QUERY_TIMEOUT_MS,
    ).result_set
    if len(incoming) > limit:
        truncated = True
        incoming = incoming[:limit]
    for rel_type, rel_props, neighbor in incoming:
        serialized = serialize_node(neighbor)
        nodes_by_id[serialized["id"]] = serialized
        edges.append(serialize_edge(serialized["id"], node_id, rel_type, rel_props))

    return {"center": center, "nodes": list(nodes_by_id.values()), "edges": edges, "truncated": truncated}


def _validated_rel_type_pattern(rel_types: Sequence[str]) -> str:
    """Relationship types can't be parametrized in Cypher (bound params only
    cover values, never labels/types -- the same constraint graph_client.py's
    write side already documents), so they get interpolated directly into the
    query string. Every caller of this module is expected to be an MCP tool
    argument or similar externally-controlled input, so each type name is
    checked against a plain identifier pattern first -- this is what stands
    between an agent-supplied edge_types list and a Cypher injection."""
    for rel_type in rel_types:
        if not _SAFE_REL_TYPE.match(rel_type):
            raise ValueError(f"invalid relationship type: {rel_type!r}")
    return "|".join(rel_types)


def impact_of(
    graph: Graph,
    node_id: str,
    direction: str = "downstream",
    max_depth: int = DEFAULT_MAX_DEPTH,
    edge_types: Sequence[str] | None = None,
    limit: int = DEFAULT_TRAVERSAL_LIMIT,
) -> dict | None:
    """Change-impact traversal: every node reachable from `node_id` within
    `max_depth` hops over `edge_types` (default: CALLS/DEPENDS_ON/READS/WRITES/
    REFERENCES -- the "this uses that" edges every ingestor writes). `direction`
    is "downstream" (what this node's changes could break, following edges
    outward), "upstream" (what could force this node to change, following edges
    inward), or "both" (each direction queried and reported separately, not
    merged, since a node reachable both ways is a genuinely different fact each
    way). Returns None if `node_id` doesn't exist. `limit` bounds each direction
    independently; `truncated` is set if either direction hit it, without ever
    silently dropping a direction's rows below its own limit."""
    center = node_summary(graph, node_id)
    if center is None:
        return None
    if direction not in ("downstream", "upstream", "both"):
        raise ValueError(f"direction must be 'downstream', 'upstream', or 'both', got {direction!r}")
    if not 1 <= max_depth <= MAX_ALLOWED_DEPTH:
        raise ValueError(f"max_depth must be between 1 and {MAX_ALLOWED_DEPTH}, got {max_depth!r}")

    type_pattern = _validated_rel_type_pattern(edge_types or DEFAULT_IMPACT_EDGE_TYPES)

    directions_to_run: list[tuple[str, str]] = []
    if direction in ("downstream", "both"):
        directions_to_run.append(("downstream", f"(n {{id: $id}})-[:{type_pattern}*1..{max_depth}]->(m)"))
    if direction in ("upstream", "both"):
        directions_to_run.append(("upstream", f"(n {{id: $id}})<-[:{type_pattern}*1..{max_depth}]-(m)"))

    impacted: list[dict] = []
    truncated = False
    for dir_label, pattern in directions_to_run:
        rows = graph.ro_query(
            f"MATCH {pattern} RETURN DISTINCT m LIMIT $limit",
            {"id": node_id, "limit": limit + 1},
        ).result_set
        if len(rows) > limit:
            truncated = True
            rows = rows[:limit]
        for (node,) in rows:
            impacted.append({"node": serialize_node_min(node), "direction": dir_label})

    return {
        "center": center,
        "direction": direction,
        "max_depth": max_depth,
        "edge_types": list(edge_types or DEFAULT_IMPACT_EDGE_TYPES),
        "impacted": impacted,
        "truncated": truncated,
    }


_VALIDATIONS_NOT_EXTRACTED_NOTE = (
    "Validation/branch-condition extraction doesn't exist in the current PL/SQL or C# "
    "ingestors -- this closure covers CALLS reachability and READS/WRITES table usage "
    "only. See CONTRIBUTING.md / README.md for the current schema."
)


def _endpoint_handlers(graph: Graph, endpoint_id: str) -> list[Node]:
    """The handler(s) that EXPOSE an APIEndpoint -- an endpoint has no outgoing CALLS of its
    own, so its behavioral closure starts from whatever code exposes it (a C# controller
    Method, a Python route-decorated Function/Method)."""
    rows = graph.ro_query(
        "MATCH (h)-[:EXPOSES_ENDPOINT]->(e:Entity {id: $id}) RETURN h",
        {"id": endpoint_id},
        timeout=DEFAULT_QUERY_TIMEOUT_MS,
    ).result_set
    return [row[0] for row in rows]


def _closure_across_seam(
    graph: Graph, root_ids: list[str], call_type_pattern: str, depth_range: str, limit: int
) -> tuple[list[dict], bool]:
    """The behavioral closure of `root_ids`, following CALLS AND transitively crossing the HTTP
    seam so an entry point's trace spans services rather than stopping at the first repo boundary.

    A call that leaves over HTTP is `caller -[:CALLS_API]-> (:APIEndpoint) <-[:EXPOSES_ENDPOINT]-
    handler`; the stitch resolves that endpoint to its handler(s) in whatever repo/language serves
    it, so we treat that triple as one "call" step and continue the CALLS closure from the handler.
    Purely additive: on a graph with no CALLS_API edges the seam pass finds nothing and the result
    is exactly the old single-query CALLS closure. Returns (nodes excluding the roots, truncated)."""
    reached: dict[str, dict] = {}
    root_set = set(root_ids)
    frontier = list(root_ids)
    frontier_seen = set(root_ids)
    truncated = False
    while frontier and len(reached) <= limit:
        rows = graph.ro_query(
            f"UNWIND $roots AS rid MATCH (n:Entity {{id: rid}})-[:{call_type_pattern}*{depth_range}]->(m) "
            "RETURN DISTINCT m LIMIT $limit",
            {"roots": frontier, "limit": limit + 1},
        ).result_set
        if len(rows) > limit:
            truncated = True
        for row in rows[:limit]:
            node = serialize_node_min(row[0])
            if node["id"] not in reached and node["id"] not in root_set:
                reached[node["id"]] = node
        # Cross the seam: any node so far that calls an endpoint reaches that endpoint's handler(s).
        seam_rows = graph.ro_query(
            "UNWIND $ids AS rid MATCH (n:Entity {id: rid})-[:CALLS_API]->(:APIEndpoint)"
            "<-[:EXPOSES_ENDPOINT]-(h) RETURN DISTINCT h",
            {"ids": list(frontier_seen | reached.keys())},
        ).result_set
        frontier = []
        for row in seam_rows:
            handler = serialize_node_min(row[0])
            if handler["id"] not in frontier_seen:
                frontier_seen.add(handler["id"])
                frontier.append(handler["id"])
                if handler["id"] not in reached and handler["id"] not in root_set:
                    reached[handler["id"]] = handler
    return list(reached.values()), truncated


def trace_entrypoint(
    graph: Graph,
    entry_id: str,
    max_depth: int | None = None,
    limit: int = DEFAULT_TRAVERSAL_LIMIT,
) -> dict | None:
    """The "rewrite closure" for a capability's entry point (a PL/SQL Procedure/
    Function or a C# Method/APIEndpoint): every function reachable via CALLS,
    plus every Table reached via READS/WRITES from the entry point or anything
    in its call closure -- crossing the real PL/SQL-to-SQL Table link where one
    exists. `max_depth` is None by default (unbounded -- "complete closure" is
    the whole point of this tool; a depth cap would silently reintroduce the
    missing-fan-out risk it exists to close), but `limit` still bounds total
    rows returned per section as a safety net against a runaway query, and
    `truncated` says so if it did.

    `validations` is always None today: no current ingestor extracts validation
    or branch-condition logic, so this cannot yet return that dimension --
    reported explicitly (with `validations_note` explaining why) rather than
    omitted, so a caller can tell "not applicable" apart from "not built yet."
    Returns None if `entry_id` doesn't exist."""
    entry = node_summary(graph, entry_id)
    if entry is None:
        return None
    if max_depth is not None and not 1 <= max_depth <= MAX_ALLOWED_DEPTH:
        raise ValueError(f"max_depth must be None or between 1 and {MAX_ALLOWED_DEPTH}, got {max_depth!r}")

    call_type_pattern = _validated_rel_type_pattern(DEFAULT_CLOSURE_CALL_EDGE_TYPES)
    depth_range = f"1..{max_depth}" if max_depth is not None else "1.."

    # An APIEndpoint is exposed BY a handler and has no outgoing CALLS itself; entering via
    # one, start the closure from its handler(s) so "the behavior of this endpoint" reaches
    # real code. Any other entry starts the closure from itself, as before.
    handler_nodes = _endpoint_handlers(graph, entry_id) if entry["label"] == "APIEndpoint" else []
    roots = [serialize_node_min(h) for h in handler_nodes] if handler_nodes else [entry]
    root_ids = [r["id"] for r in roots]

    # CALLS closure from the root(s), transitively crossing the HTTP seam so the trace is truly
    # end to end -- a checkout that hops React -> C# gateway -> Python platform reaches the
    # money-moving code, not just the first service's handler.
    closure, functions_truncated = _closure_across_seam(
        graph, root_ids, call_type_pattern, depth_range, limit
    )
    # When entering via an endpoint, the handler(s) themselves are part of the behavior.
    entered_handlers = [r for r in roots if r["id"] != entry_id] if handler_nodes else []
    functions = entered_handlers + closure
    closure_ids = root_ids + [f["id"] for f in closure]

    data_type_pattern = _validated_rel_type_pattern(DEFAULT_CLOSURE_DATA_EDGE_TYPES)
    table_rows = graph.ro_query(
        f"""
        UNWIND $ids AS fid
        MATCH (n:Entity {{id: fid}})-[rel:{data_type_pattern}]->(t)
        RETURN DISTINCT t, type(rel)
        LIMIT $limit
        """,
        {"ids": closure_ids, "limit": limit + 1},
    ).result_set
    tables_truncated = len(table_rows) > limit
    tables_read = []
    tables_written = []
    for table, rel_type in table_rows[:limit]:
        (tables_read if rel_type == "READS" else tables_written).append(serialize_node_min(table))

    return {
        "entry": entry,
        "functions": functions,
        "tables_read": tables_read,
        "tables_written": tables_written,
        "validations": None,
        "validations_note": _VALIDATIONS_NOT_EXTRACTED_NOTE,
        "max_depth": max_depth,
        "truncated": functions_truncated or tables_truncated,
    }


DEFAULT_CALL_DEPTH = 1


def _call_neighbors(graph: Graph, node_id: str, direction: str, max_depth: int, limit: int) -> dict | None:
    """Shared core for callers()/callees(): CALLS neighbours of `node_id` in one direction,
    up to `max_depth` hops (1 = direct)."""
    center = node_summary(graph, node_id)
    if center is None:
        return None
    if not 1 <= max_depth <= MAX_ALLOWED_DEPTH:
        raise ValueError(f"max_depth must be between 1 and {MAX_ALLOWED_DEPTH}, got {max_depth!r}")
    # callers = who reaches this (inward); callees = what this reaches (outward). CALLS is an
    # in-process call; INVOKES is the HTTP stitch's derived cross-service call (caller -> handler),
    # so "who calls X" spans the request/response seam across repos and languages, not just one repo.
    pattern = f"<-[:CALLS|INVOKES*1..{max_depth}]-" if direction == "callers" else f"-[:CALLS|INVOKES*1..{max_depth}]->"
    rows = graph.ro_query(
        f"MATCH (n:Entity {{id: $id}}){pattern}(m) RETURN DISTINCT m LIMIT $limit",
        {"id": node_id, "limit": limit + 1},
        timeout=DEFAULT_QUERY_TIMEOUT_MS,
    ).result_set
    truncated = len(rows) > limit
    # The connecting CALLS edges along the reachable paths, in the canonical directed shape (every
    # edge is caller -> callee via startNode/endNode, regardless of which way we traversed) so the
    # result shows the actual call chain, not just a flat set of reached nodes.
    edge_limit = limit * max_depth
    edge_rows = graph.ro_query(
        f"MATCH p=(n:Entity {{id: $id}}){pattern}(m) UNWIND relationships(p) AS r "
        "RETURN DISTINCT startNode(r).id, endNode(r).id, type(r), properties(r) LIMIT $elimit",
        {"id": node_id, "elimit": edge_limit + 1},
        timeout=DEFAULT_QUERY_TIMEOUT_MS,
    ).result_set
    edges = [serialize_edge_lean(f, t, ty, p) for f, t, ty, p in edge_rows[:edge_limit]]
    return {
        "center": center,
        "direction": direction,
        "max_depth": max_depth,
        "nodes": [serialize_node_min(row[0]) for row in rows[:limit]],
        "edges": edges,
        "edges_truncated": len(edge_rows) > edge_limit,
        "truncated": truncated,
    }


def callers(graph: Graph, node_id: str, max_depth: int = DEFAULT_CALL_DEPTH, limit: int = DEFAULT_TRAVERSAL_LIMIT) -> dict | None:
    """Every function/method that CALLS `node_id` (inward), within `max_depth` hops (1 =
    direct callers). None if the node doesn't exist."""
    return _call_neighbors(graph, node_id, "callers", max_depth, limit)


def callees(graph: Graph, node_id: str, max_depth: int = DEFAULT_CALL_DEPTH, limit: int = DEFAULT_TRAVERSAL_LIMIT) -> dict | None:
    """Every function/method that `node_id` CALLS (outward), within `max_depth` hops (1 =
    direct callees). None if the node doesn't exist."""
    return _call_neighbors(graph, node_id, "callees", max_depth, limit)


DEFAULT_PATH_DEPTH = 6


def shortest_path(
    graph: Graph,
    from_id: str,
    to_id: str,
    max_depth: int = DEFAULT_PATH_DEPTH,
    directed: bool = True,
    edge_types: Sequence[str] | None = None,
) -> dict | None:
    """The shortest path between two nodes (<= `max_depth` hops) -- "how are these
    two entities connected?". The single path primitive behind the viewer's Path
    finder and the MCP `find_path` tool.

    `edge_types` scopes which relationships may be traversed: None (default) means
    *any* type; a list restricts it -- e.g. ``["CALLS"]`` gives a pure call chain.
    `directed` follows
    edge direction (from_id -> ... -> to_id); when False, edges are traversed
    either way, so a connection is found regardless of which way its arrows point.

    Returns both the ordered node list AND the connecting edges (each with its
    real src->dest direction and type) so the viewer can highlight the whole path
    with its relationship labels. `found` is False when no path exists within the
    depth bound; None if either endpoint doesn't exist.

    Deliberately avoids the `... RETURN p ORDER BY length(p) LIMIT 1` idiom:
    `ORDER BY length(p)` forces FalkorDB to materialise *every* path up to
    `max_depth` before it can pick the shortest, which blows up (and times out)
    on a dense hub. Two short-circuiting strategies instead:
      - directed: FalkorDB's native `shortestPath()` -- a BFS that stops at the
        first (shortest) path. It is only valid in a WITH/RETURN clause, not MATCH.
      - undirected: FalkorDB has no undirected `shortestPath()`, so iterate the
        depth d = 1, 2, ... and take the first *exactly-d* match with `LIMIT 1`
        (no ORDER BY, so the engine returns the first path it finds and stops).
        The first d that yields anything is by construction the shortest, and the
        common case -- a short path -- is found without ever exploring the deep
        frontier. The query timeout still backstops a pathological deep search."""
    if not 1 <= max_depth <= MAX_ALLOWED_DEPTH:
        raise ValueError(f"max_depth must be between 1 and {MAX_ALLOWED_DEPTH}, got {max_depth!r}")
    start = node_summary(graph, from_id)
    end = node_summary(graph, to_id)
    if start is None or end is None:
        return None

    type_pattern = _validated_rel_type_pattern(edge_types) if edge_types else ""
    inner = f":{type_pattern}" if type_pattern else ""  # "" or ":CALLS|WRITES"
    echo_types = list(edge_types) if edge_types else None

    path = None
    if directed:
        rows = graph.ro_query(
            f"MATCH (a:Entity {{id: $from}}), (b:Entity {{id: $to}}) "
            f"WITH shortestPath((a)-[{inner}*1..{max_depth}]->(b)) AS p RETURN p",
            {"from": from_id, "to": to_id},
            timeout=DEFAULT_QUERY_TIMEOUT_MS,
        ).result_set
        if rows and Path is not None and isinstance(rows[0][0], Path):
            path = rows[0][0]
    else:
        for depth in range(1, max_depth + 1):
            rows = graph.ro_query(
                f"MATCH p = (a:Entity {{id: $from}})-[{inner}*{depth}..{depth}]-(b:Entity {{id: $to}}) "
                "RETURN p LIMIT 1",
                {"from": from_id, "to": to_id},
                timeout=DEFAULT_QUERY_TIMEOUT_MS,
            ).result_set
            if rows and Path is not None and isinstance(rows[0][0], Path):
                path = rows[0][0]
                break

    if path is None:
        return {
            "from": start, "to": end, "directed": directed, "edge_types": echo_types,
            "found": False, "hops": 0, "nodes": [], "edges": [],
        }

    # NB: serialize_node_LEAN (not _min) here -- shortest_path also backs the viewer's /path endpoint,
    # whose nodes replace cached data the inspector reads from node.properties; min would strip the
    # id/qualified_name/signature the viewer shows. Paths are short, so the token cost is negligible.
    path_nodes = [serialize_node_lean(n) for n in path.nodes()]
    # A relationship carries only its endpoints' *internal* ids; map them back to
    # the real string ids via the path's own nodes so each edge renders with its
    # true direction (which, for an undirected match, may run opposite to the way
    # the path was walked).
    internal_to_real = {n.id: n.properties.get("id") for n in path.nodes()}
    edges = [
        {
            "from": internal_to_real.get(e.src_node),
            "to": internal_to_real.get(e.dest_node),
            "type": e.relation,
            "properties": _visible_properties(dict(e.properties)),
        }
        for e in path.edges()
    ]
    return {
        "from": start,
        "to": end,
        "directed": directed,
        "edge_types": echo_types,
        "found": True,
        "hops": len(path_nodes) - 1,
        "nodes": path_nodes,
        "edges": edges,
    }


def _meta_stats(graph: Graph) -> dict | None:
    """FalkorDB's native `CALL db.meta.stats()` -- label + edge-type counts (and totals)
    maintained by the engine, so no full scan. Returns None if the procedure is unsupported,
    so callers fall back to MATCH-based counts."""
    try:
        rows = graph.ro_query("CALL db.meta.stats()", timeout=DEFAULT_QUERY_TIMEOUT_MS).result_set
    except Exception:
        return None
    if not rows:
        return None
    cells = rows[0]

    def _as_dict(cell) -> dict:
        try:
            return dict(cell)
        except Exception:
            return {}

    return {
        "label_counts": _as_dict(cells[0]) if len(cells) > 0 else {},
        "edge_counts": _as_dict(cells[1]) if len(cells) > 1 else {},
        "num_edges": cells[2] if len(cells) > 2 else None,
        "num_nodes": cells[3] if len(cells) > 3 else None,
    }


def graph_schema(graph: Graph) -> dict:
    """The graph's meta-model, so an agent understands its STRUCTURE (not just counts):
    node labels, edge types, the actual `(FromLabel)-[TYPE]->(ToLabel)` patterns present
    (the schema graph), and the property keys each node label carries. This is what lets an
    agent write a correct `run_cypher` query, or know which traversal answers a question."""
    stats = _meta_stats(graph)
    if stats is not None:
        label_counts = [(label, count) for label, count in stats["label_counts"].items() if count]
        edge_rows = [(rel, count) for rel, count in sorted(stats["edge_counts"].items(), key=lambda kv: -kv[1]) if count]
    else:
        try:
            label_counts = _label_counts(graph)
        except Exception:
            return {"node_labels": [], "edge_types": [], "relationship_patterns": [], "properties_by_label": {}}
        edge_rows = graph.ro_query(
            "MATCH ()-[r]->() RETURN type(r), count(r) ORDER BY count(r) DESC",
            timeout=DEFAULT_QUERY_TIMEOUT_MS,
        ).result_set

    # The schema graph: which labels are connected by which edge type, and how often. This
    # is the single most useful thing for reasoning about a code graph's shape.
    pattern_rows = graph.ro_query(
        f"MATCH (a)-[r]->(b) RETURN {semantic_label('a')}, type(r), {semantic_label('b')}, "
        "count(*) AS c ORDER BY c DESC LIMIT 200",
        timeout=DEFAULT_QUERY_TIMEOUT_MS,
    ).result_set

    # Property keys per label -- sampled from one node each (drop internal `_`-prefixed keys).
    properties_by_label: dict[str, list[str]] = {}
    for label, _count in label_counts:
        if not label or not _SAFE_REL_TYPE.match(label):
            continue
        rows = graph.ro_query(
            f"MATCH (n:{label}) RETURN keys(n) LIMIT 1", timeout=DEFAULT_QUERY_TIMEOUT_MS
        ).result_set
        keys = rows[0][0] if rows and rows[0][0] else []
        properties_by_label[label] = sorted(k for k in keys if not _is_internal_key(k))

    return {
        "node_labels": [{"label": label, "count": count} for label, count in label_counts],
        "edge_types": [{"type": row[0], "count": row[1]} for row in edge_rows],
        "relationship_patterns": [
            {"from": frm, "type": rel, "to": to, "count": cnt} for frm, rel, to, cnt in pattern_rows
        ],
        "properties_by_label": properties_by_label,
    }


def graph_status(graph: Graph) -> dict:
    """Whether this graph holds anything yet, its label counts, and which git-tracked
    projects (repos) are in it -- what an agent checks before deciding to index vs query."""
    stats = _meta_stats(graph)
    if stats is not None:
        counts = [(label, count) for label, count in stats["label_counts"].items() if count]
        total = stats["num_nodes"] if stats["num_nodes"] is not None else sum(c for _l, c in counts)
    else:
        try:
            counts = _label_counts(graph)
        except Exception:  # a never-written graph key -- RO_QUERY rejects it ("empty key")
            return {"has_data": False, "node_count": 0, "labels": [], "projects": []}
        total = sum(count for _label, count in counts)
    return {
        "has_data": total > 0,
        "node_count": total,
        "labels": [{"label": label, "count": count} for label, count in counts],
        "projects": git_tracked_projects(graph) if total else [],
    }


def graphs_overview(client: FalkorDB, current: str | None = None) -> dict:
    """The landscape across the WHOLE FalkorDB instance: every code graph, the repositories it
    represents, and its size. This is what an agent reads before indexing another repository --
    to see which graphs already exist and what each one holds, so it can decide whether the new
    repo extends an existing graph or belongs in a dedicated one.

    `current` marks (and is echoed back for) the graph the caller is bound to. Node counts come
    from the engine's maintained stats (no full scan); a graph whose key was never written is
    skipped. Returns `{current, graphs: [{name, current, node_count, repositories}]}`."""
    graphs: list[dict] = []
    for name in sorted(client.list_graphs()):
        graph = client.select_graph(name)
        stats = _meta_stats(graph)
        if stats is not None:
            node_count = stats["num_nodes"]
            if node_count is None:
                node_count = sum(stats["label_counts"].values())
        else:
            try:
                node_count = sum(count for _label, count in _label_counts(graph))
            except Exception:  # a never-written graph key -- RO_QUERY rejects it ("empty key")
                continue
        graphs.append(
            {
                "name": name,
                "current": name == current,
                "node_count": node_count,
                "repositories": repositories(graph),
            }
        )
    return {"current": current, "graphs": graphs}


def detect_communities(graph: Graph, ids: Sequence[str] | None = None) -> dict:
    """Community detection over the WHOLE graph via FalkorDB's label-propagation
    procedure (`algo.labelPropagation`, read-only -- it YIELDs node -> communityId,
    it does NOT write). LPA here is deterministic, so re-runs give identical
    community ids (colours stay stable as the viewer expands). The algorithm
    always runs over the entire graph; `ids`, when given, scopes only the
    RETURNED map to the caller's on-canvas nodes, so a large graph doesn't ship
    250k assignments just to colour a few hundred visible ones. Returns
    {communities: {node_id: community_id}, community_count}."""
    if ids:
        rows = graph.ro_query(
            "CALL algo.labelPropagation(NULL) YIELD node, communityId "
            "WITH node, communityId WHERE node.id IN $ids "
            "RETURN node.id AS id, communityId",
            {"ids": list(ids)},
            timeout=DEFAULT_QUERY_TIMEOUT_MS,
        ).result_set
    else:
        rows = graph.ro_query(
            "CALL algo.labelPropagation(NULL) YIELD node, communityId RETURN node.id AS id, communityId",
            timeout=DEFAULT_QUERY_TIMEOUT_MS,
        ).result_set
    communities = {row[0]: row[1] for row in rows if row[0] is not None}
    return {"communities": communities, "community_count": len(set(communities.values()))}
