"""FalkorDB connection handling and idempotent graph writes.

Node/edge labels and relationship types are always interpolated straight into the
Cypher text below. That's safe *only* because they only ever come from our own
NodeSchema/EdgeSchema/GraphNode/GraphEdge definitions, never from raw external input --
Cypher has no way to parametrize a label or relationship type, so this is the
standard approach. All property *values* are passed as bound query parameters.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections import defaultdict
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # annotations only (module has `from __future__ import annotations`) -- see connect_client
    from falkordb import FalkorDB, Graph
from redis.exceptions import RedisError, ResponseError

from auryn.core.graph_model import GraphEdge, GraphNode, NodeSchema
from auryn.core.graph_sync import (
    SYNC_MARKER,
    EdgeKey,
    GraphDelta,
    NodeKey,
    edge_key,
    fingerprint,
    node_key,
    unstamped,
)
from auryn.core.instrument import phase

logger = logging.getLogger(__name__)


# ── value-level string interning ──────────────────────────────────────────────────────
# FalkorDB's intern() stores one shared copy of a repeated string value. Measured caveat: it
# only dedupes when the value is assigned ONCE via intern() and never stored plain first --
# `SET n += props` then re-interning ("override") allocates+frees a buffer per row and saves
# nothing (it even worsens RSS fragmentation). So interned props are EXCLUDED from the `+= props`
# map and set solely through `intern(row.iv.<k>)` below. Applied only to low-cardinality
# "enum-like" props that repeat across many entities; unique props (id, qualified_name, source,
# ...) are excluded, since interning them only adds pointer overhead. intern() is value-
# transparent -- an interned string reads/compares identically to a raw one -- so query results
# and the delta==full invariant are unchanged. Set AURYN_INTERN=0 to disable.
_INTERN_NODE_PROPS = frozenset({
    "kind", "type", "type_source", "return_type", "return_type_source",
    "accessibility", "visibility", "origin", "dialect", "defined_in",
    "provenance", "framework", "http_method",
})
# Edges also intern `_fingerprint`: a long hash, constant on property-less containment edges and
# low-cardinality on others, so it dedupes strongly. Node `_fingerprint`s are near-unique, so
# interning them would be pure overhead -- they stay plain.
_INTERN_EDGE_PROPS = frozenset({"resolution", "kind", "receiver_type", "_fingerprint"})


def _interning_enabled() -> bool:
    """String-value interning is on by default; AURYN_INTERN=0 disables it."""
    return os.getenv("AURYN_INTERN", "1") != "0"


def _intern_clause(var: str, rows: list[dict]) -> str:
    """SET-clause suffix that assigns each chunk's interned props via intern(row.iv.<k>). Emits
    only interned props present somewhere in the chunk; intern(null) is a no-op, so a row missing
    one is unaffected. Interned props are absent from `row.plain`, so they're never stored plain."""
    return "".join(
        f", {var}.`{k}` = intern(row.iv.`{k}`)"
        for k in sorted({k for r in rows for k in r["iv"]})
    )


_RESULTSET_SIZE = 1_000_000
"""FalkorDB's RESULTSET_SIZE config defaults to 10000 and silently truncates any
query result past that many rows -- regardless of what the query itself asks for
(confirmed empirically: even a plain MATCH...RETURN with no ORDER BY/SKIP/LIMIT at
all still comes back capped at 10000 rows, no error or warning). A real project's
per-label node/edge count blows straight through that (dotnet/aspnetcore has
~271k DEPENDS_ON edges from Method alone) -- discovered via a benchmark run that
reported a suspiciously enormous nodes_added count with zero nodes_removed.
setup_schema() raises this once, idempotently, rather than working around the cap
with SKIP/LIMIT pagination: paginating a query that has an ORDER BY (needed for
pagination to be deterministic across calls -- confirmed a plain SKIP/LIMIT
without one silently drops rows) means every page re-sorts the *entire* matching
set just to skip to a different window, and running pages concurrently only
turns that redundant work into redundant work in parallel, not less work. Setting
this instead lets read_fingerprints() issue one plain, unordered query per label.
This is a server-wide config, not scoped to one graph -- 1,000,000 is comfortably
above any real per-label count seen so far, chosen over -1 (unlimited) to keep
some ceiling against a truly pathological query."""

_SUBGRAPH_QUERY_TIMEOUT_MS = 30_000
"""FalkorDB's own query TIMEOUT config defaults to 1000ms. Confirmed via
GRAPH.EXPLAIN that an unlabeled `MATCH (n) WHERE n.id STARTS WITH ...` (or the
edge equivalent) never uses the range index setup_schema() creates on each
label's `id` -- it plans as "All Node Scan", full stop, regardless of the WHERE
clause, since FalkorDB's indexes are per-label and an unlabeled match can't
consult one. That's why _read_node_fingerprints/_read_edge_fingerprints below
query per-label (see _owned_ids) -- confirmed via GRAPH.EXPLAIN to plan as a
"Node By Label Scan" instead. This timeout is generous headroom for a single
large label's full, unordered read (no sort -- see _RESULTSET_SIZE) on a
substantially larger repo than what this was profiled against."""

_SUBGRAPH_READ_WORKERS = 16
"""read_fingerprints() issues one query per (label, node-or-edge) pair -- all
mutually independent reads against an unchanging graph (nothing writes in
between), so there's no correctness reason to serialize them. Profiling a real
incremental resync against dotnet/aspnetcore found this read was ~87% of total
sync time (42s of 48s) -- reading back the whole current graph to diff against
dominates a sync even when the actual delta is tiny. falkordb-py wraps a standard
redis.Redis client, which pools connections and is safe for concurrent use across
threads (confirmed empirically). Capped rather than one-worker-per-label since
FalkorDB is a single server process -- more threads than this stopped helping in
testing and just adds contention."""

_INDEX_BUILD_TIMEOUT_S = 300.0
"""How long await_indexes() waits for FalkorDB to finish building an index it just created.
The build runs in the background after CREATE INDEX returns, and a search against the index
meanwhile returns NOTHING. That gap is why a search right after an embed pass came back empty --
on a loaded CI runner even for three nodes. Measured with the tests' small fake vectors it builds
about 14k nodes a second on a laptop (~6 ms for 300 nodes, ~0.4 s for 6,000); real 384- or
768-dimension vectors build slower. Five minutes is headroom, not a guarantee: past it the pass
reports the index as still building."""

_INDEX_POLL_INTERVAL_S = 0.02
_INDEX_POLL_MAX_INTERVAL_S = 1.0
"""await_indexes() looks every 20 ms at first -- a small index is built within a look or two --
and doubles the pause up to a second, so a long build costs a few hundred `db.indexes()` calls
rather than thousands."""

_WRITE_BATCH_SIZE = 2000
"""One UNWIND-based write per this many entities rather than one query per entity --
confirmed empirically to be ~150x faster once the standard unique-constraint index
exists (5000 individual MERGE queries: ~2.9s; one batched UNWIND MERGE: ~0.02s).
Chunked (rather than one query for an arbitrarily large list) to keep any single
request/response small regardless of how big a sync's delta is."""

_WRITE_QUERY_TIMEOUT_MS = 30_000
"""Per-query timeout every write carries, overriding FalkorDB's server-wide default (the
`falkordb/falkordb` image ships `TIMEOUT 1000`, i.e. 1s). A single large write batch -- the deep
(semantic) pass of a big repo lands tens of thousands of inferred-type / resolved-call edges -- can
exceed 1s, and the write would then die mid-pass with `Query timed out`, leaving a silently shallow
graph (#857). The read path already overrides the same 1s default (see _SUBGRAPH_QUERY_TIMEOUT_MS);
this is its write-side counterpart. A per-query timeout that FalkorDB accepts on the reads is
accepted here too; where a server pins TIMEOUT_MAX below a batch's need, _write_rows() additionally
splits-and-retries so the pass still completes rather than erroring."""


def _chunked(items: list, size: int) -> Iterator[list]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


# A small stopword set for the lexical (full-text) ranker -- dropped so a natural-language query
# ("how do I open a database connection") reduces to its content terms. RediSearch has its own
# stopword list too; this just keeps the OR-query focused.
_FT_STOPWORDS = frozenset(
    {"a", "an", "the", "to", "of", "for", "and", "or", "in", "on", "how", "do", "i", "we",
     "is", "it", "this", "that", "with", "my", "me", "you", "your", "where", "what", "which"}
)


def _fulltext_tokens(query: str) -> list[str]:
    """Content tokens for a full-text query: identifier/word runs, lowercased, stopwords and
    single chars dropped. Sanitizing to `[A-Za-z0-9_]` also makes the tokens safe to OR-join
    into a RediSearch query without escaping its operator characters."""
    toks = re.findall(r"[A-Za-z0-9_]+", query.lower())
    return [t for t in toks if len(t) >= 2 and t not in _FT_STOPWORDS]


def _owned_ids(id_prefix: str) -> dict[str, str]:
    """Query params selecting exactly the ids `id_prefix` owns: the id itself (`$own`) or one
    that continues it at a boundary -- `/` (`$sub`: `<proj>/file/...`, `<proj>/module/...`) or `.`
    (`$dot`: a top-level `<proj>.class.x` / `<proj>.func.f()`, every PL/SQL id). The fingerprint
    reads use this rather than the indexed range `[prefix, prefix + chr(0xFFFF))` because whatever
    they return and the target doesn't re-emit gets DELETED, and the range is not a project
    boundary: it also covers a sibling project (`python://api` ... `python://api2/...`), and
    FalkorDB 4.x -- the engine falkordblite embeds -- returns wrong rows for an indexed string
    range when ids differ in letter case. Either way a resync of one project deleted another's
    nodes. `STARTS WITH` is exact on every engine; it costs the index (a per-label scan + filter,
    ~1.4x on a 500k-node label shared by 20 projects). A name that itself ends in `/` or `.` is
    no special case: `cpp://api.` owns `cpp://api./...` and `cpp://api..class.x`, never
    `cpp://api.2`. Both rules also match another project whose NAME continues this one's at a
    boundary (`Acme.Core` / `Acme.Core.Tests`, `csharp://Sol` / `csharp://Sol/Core`); the reads
    drop what that one owns -- see GraphWriter._nested_projects."""
    return {"own": id_prefix, "sub": id_prefix + "/", "dot": id_prefix + "."}


def _owned_by(node_id: str, project_ids: tuple[str, ...]) -> bool:
    """Whether `node_id` is one of `project_ids` or continues it at a `/` or `.` boundary."""
    return any(node_id == p or node_id.startswith((p + "/", p + ".")) for p in project_ids)


def _node_fingerprint_query(label: str) -> str:
    """read_fingerprints()'s whole node read for `label`, one plain query -- pulled
    out as its own function (rather than inlined in _read_node_fingerprints) so a
    test can GRAPH.EXPLAIN this exact query shape and assert it actually plans as
    a per-label scan. Fetches only `id` and the stored `_fingerprint` (see
    graph_sync.fingerprint()) rather than full properties(n) -- confirmed
    empirically ~4x faster at realistic scale (properties(n) can include a
    multi-KB `source` text field; a fingerprint is a fixed-size hash regardless).
    No ORDER BY/SKIP/LIMIT: see _RESULTSET_SIZE for why paginating this would be
    worse, not better."""
    return (
        f"MATCH (n:{label}) WHERE n.id = $own OR n.id STARTS WITH $sub OR n.id STARTS WITH $dot "
        "RETURN n.id, n._fingerprint"
    )


# ── system-wide labels (never surfaced to a caller) ────────────────────────────────────
# Every node carries, besides its dedicated (semantic) label -- File/Function/Class/... -- one or
# more *system* labels that exist purely so a single index can span every node kind:
#   * SHARED_NODE_LABEL   -- an id range index, so a by-id lookup that can't name the label (the MCP
#                            neighbors/callers/callees/path/get_node tools) is an index seek, not an
#                            all-node scan (a label-less `MATCH (n {id:$id})` uses no index).
#   * SHARED_EMBEDDING_LABEL -- the shared vector/full-text index (semantic search), added only to
#                            embedded nodes.
# FalkorDB returns ALL of a node's labels regardless of which the query named, so anything handed
# back to a caller must strip these: a consumer sees `File`, never `File`+`Entity`. `semantic_label`
# (Cypher) and `first_semantic_label` (Python) are the single choke points that do the stripping.
SHARED_NODE_LABEL = "Entity"
SHARED_EMBEDDING_LABEL = "Embeddable"
_SHARED_LABELS = (SHARED_NODE_LABEL, SHARED_EMBEDDING_LABEL)


def semantic_label(var: str) -> str:
    """Cypher expression for a node's dedicated label: the first label that is not a system label.
    Use everywhere a query would otherwise `RETURN labels(n)[0]` -- with the system labels present,
    `labels(n)[0]` is order-dependent and may hand the caller `Entity`."""
    conds = " AND ".join(f"l <> '{lbl}'" for lbl in _SHARED_LABELS)
    return f"[l IN labels({var}) WHERE {conds}][0]"


def first_semantic_label(labels) -> str | None:
    """Python-side counterpart of `semantic_label` for a falkordb Node's `.labels` list."""
    for lbl in labels or ():
        if lbl not in _SHARED_LABELS:
            return lbl
    return None


def _edge_fingerprint_query(from_label: str) -> str:
    """read_fingerprints()'s whole edge read for `from_label` -- see
    _node_fingerprint_query's docstring for why this is its own function and why
    only `_fingerprint` (not full properties(r)) is fetched. The target label is stripped of the
    system labels (semantic_label) so the edge fingerprint key matches the semantic to_label the
    target items carry -- otherwise every edge would look changed.

    The target filter admits three id spaces: this project's own prefix (its internal edges), the
    `extern://<lang>/` call placeholders (ADR 0004 -- libc/BCL/framework/cross-repo), and the
    `image://` deployment-tier hub (ADR 0007). A placeholder/hub node lives OUTSIDE every project
    prefix, yet a `CALLS -> extern://` or `BASED_ON -> image://` edge is AUTHORED by this project (its
    source is in-prefix) and fully re-emitted each recompute, so it must be diffed here -- otherwise a
    caller that drops an unresolved call (or a Dockerfile that changes its `FROM`) leaves a stale edge
    the full-recompute diff never removes. Both the `extern://` and `image://` target NODES are shared
    and GC'd elsewhere (the linkage reconciler / the deploy stitch), not by this per-project diff.

    Two id spaces are deliberately still EXCLUDED: (1) a reconciled cross-repo edge (source here, target a
    DIFFERENT real project), owned by the linkage reconciler not this per-project diff; (2) `extern://http/`
    endpoint nodes (ADR 0006), a SEAM namespace whose edges are authored by the http-endpoints HOOK (and
    its own dedup/GC), not by any ingestor's resolve -- diffing them here would churn hook-owned edges the
    target never emits (and drop them entirely if the hook is disabled). The call-placeholder namespaces
    (c/csharp/python/go/rust/...) are each fully re-emitted by their ingestor's resolve, so are safe to diff."""
    return (
        f"MATCH (a:{from_label}) WHERE a.id = $own OR a.id STARTS WITH $sub OR a.id STARTS WITH $dot "
        "MATCH (a)-[r]->(b) WHERE b.id = $own OR b.id STARTS WITH $sub OR b.id STARTS WITH $dot "
        "   OR (b.id STARTS WITH 'extern://' AND NOT b.id STARTS WITH 'extern://http/') "
        "   OR b.id STARTS WITH 'image://' "
        f"RETURN type(r), {semantic_label('b')}, a.id, b.id, r._fingerprint"
    )


@dataclass(frozen=True)
class FalkorDBConfig:
    host: str = "localhost"
    port: int = 6379
    username: str | None = None
    password: str | None = None
    ssl: bool = False
    graph_name: str = "auryn"
    # Embedded, serverless mode: run FalkorDB in-process (a bundled Redis + the FalkorDB
    # module, via the optional `falkordblite` package) instead of connecting to a running
    # server -- nothing to start, no Docker. `db_path` is the file the graph persists to
    # (reusing a path reuses the graph); when embedded, the host/port/auth fields above
    # are ignored. Because one embedded instance is owned by a single process, embedded
    # mode is for a single owning process (e.g. `auryn quickstart`), not for separate
    # `view`+`mcp` processes sharing one file.
    embedded: bool = False
    db_path: str | None = None


def connect_client(config: FalkorDBConfig) -> FalkorDB:
    """A FalkorDB client for `config`: normally a network client to a running server;
    when `config.embedded` is set, an in-process serverless client instead (see
    _connect_embedded). The two are duck-compatible -- same select_graph/query/ro_query/
    execute_command surface -- so everything downstream (GraphWriter, the viewer, the MCP
    server) is unchanged regardless of which one this returns."""
    from falkordb import FalkorDB  # deferred: importing falkordb->redis costs ~55ms of CLI startup

    if config.embedded:
        return _connect_embedded(config.db_path)
    return FalkorDB(
        host=config.host,
        port=config.port,
        username=config.username,
        password=config.password,
        ssl=config.ssl,
    )


def _connect_embedded(db_path: str | None) -> FalkorDB:
    """An embedded, serverless FalkorDB: `falkordblite` bundles a Redis + the FalkorDB
    module and manages them in-process, so there's no separate server or Docker to run.
    The graph persists to `db_path` (a plain file) -- reusing the same path across runs
    reuses the graph; None uses a throwaway location. falkordblite is an OPTIONAL
    dependency, auto-installed on demand (opt-out; AURYN_AUTO_INSTALL=0 to disable),
    exactly like the live-DB drivers -- a server-only deploy never pays for it. Its client
    class mirrors falkordb-py's, so the returned object is duck-compatible with the network
    client above (verified against the full GraphWriter/viewer surface)."""
    from auryn.core.plugins import ensure_importable

    module = ensure_importable("redislite.falkordb_client", "falkordblite>=0.10")
    return module.FalkorDB(db_path)


def connect(config: FalkorDBConfig) -> Graph:
    return connect_client(config).select_graph(config.graph_name)


class GraphWriter:
    """Wraps a FalkorDB Graph with idempotent (MERGE-based) node/edge writes."""

    def __init__(self, graph: Graph) -> None:
        self._graph = graph

    def sibling(self) -> "GraphWriter":
        """A second writer bound to the *same* graph via a fresh Graph object on the same
        (pool-backed, thread-safe) client. Used to run a background deep-ingest pass on its
        own Graph while the caller keeps using this one -- a FalkorDB Graph is a thin wrapper
        over the pooled client (its only mutable state is a cached schema map), so a fresh one
        never shares that state.

        Reconstructs the Graph directly (`type(g)(g.client, g._name)`) rather than via
        `g.client.select_graph(...)` so it's backend-agnostic: on the server client
        `g.client` is the FalkorDB wrapper (this is exactly what select_graph would build),
        but on the embedded falkordblite client `g.client` is the raw Redis, which has no
        select_graph. Both backends' Graph constructor is `(client, name)`, so passing each
        Graph's own `client` back reproduces a fresh handle on either."""
        return GraphWriter(type(self._graph)(self._graph.client, self._graph._name))

    def setup_schema(self, node_schemas: list[NodeSchema]) -> None:
        """Ensure the indexes/constraints these node schemas need exist, and that
        this FalkorDB instance's RESULTSET_SIZE is raised enough for
        read_fingerprints() to read back a large label/edge-group in one
        unpaginated query (see _RESULTSET_SIZE). Safe to call repeatedly --
        creation calls that fail because the index/constraint already exists are
        treated as success, and GRAPH.CONFIG SET is idempotent by nature."""
        self._graph.execute_command("GRAPH.CONFIG", "SET", "RESULTSET_SIZE", _RESULTSET_SIZE)
        for schema in node_schemas:
            for prop in schema.indexed_properties:
                self._create_range_index(schema.label, prop)
            self._create_unique_constraint(schema.label, schema.key_property)
        self._ensure_shared_node_index()

    def _ensure_shared_node_index(self) -> None:
        """Create the range index on the system-wide :Entity(id) that makes a label-less by-id
        lookup an index seek (see SHARED_NODE_LABEL). New graphs get the label at write time; a
        graph written before this existed is back-filled ONCE -- tied to the index not existing yet,
        so the one-time full-node relabel runs exactly once per graph and never again (the relabel is
        a no-op on an empty new graph)."""
        created = False
        try:
            self._graph.create_node_range_index(SHARED_NODE_LABEL, "id")
            created = True
        except ResponseError:
            pass  # already exists -> already back-filled
        if created:
            # Pre-existing nodes (from before the shared label) get it now; new/empty graphs no-op.
            self._write(f"MATCH (n) WHERE n.id IS NOT NULL SET n:{SHARED_NODE_LABEL}")

    def _create_range_index(self, label: str, prop: str) -> None:
        try:
            self._graph.create_node_range_index(label, prop)
            logger.debug("Created range index on :%s(%s)", label, prop)
        except ResponseError:
            logger.debug("Range index on :%s(%s) already exists", label, prop)

    def _create_unique_constraint(self, label: str, key_property: str) -> None:
        try:
            # This also creates the backing range index on key_property if missing
            # (falkordb-py does this internally), so no separate call is needed here.
            self._graph.create_node_unique_constraint(label, key_property)
            logger.debug("Created unique constraint on :%s(%s)", label, key_property)
        except ResponseError:
            logger.debug("Unique constraint on :%s(%s) already exists", label, key_property)

    def create_vector_index(self, label: str, prop: str, dim: int, *, similarity: str = "cosine") -> None:
        """Ensure a FalkorDB vector index exists on :label(prop) for `dim`-dimensional vectors.
        Idempotent (an already-indexed attribute raises ResponseError, treated as success), like
        the range/constraint helpers. `cosine` distance is the default -- the queryNodes score is
        then a distance (0 == identical), so results sort ascending by score. The index is
        fixed-dimension: changing the embedding model means drop_vector_index + recompute (see
        core/embeddings/embedder.py)."""
        try:
            self._graph.create_node_vector_index(label, prop, dim=dim, similarity_function=similarity)
            logger.debug("Created vector index on :%s(%s) dim=%d %s", label, prop, dim, similarity)
        except ResponseError as e:
            if "already" not in str(e).lower():  # a real failure, not the idempotent "already indexed"
                raise  # don't swallow it -- a missing index must not be reported as ready (await_indexes)
            logger.debug("Vector index on :%s(%s) already exists", label, prop)

    def drop_vector_index(self, label: str, prop: str) -> None:
        """Drop the vector index on :label(prop) if present (idempotent). Used when switching
        embedding models, where the whole vector space -- index and every stored vector -- is
        rebuilt from scratch."""
        try:
            self._graph.drop_node_vector_index(label, prop)
            logger.debug("Dropped vector index on :%s(%s)", label, prop)
        except ResponseError:
            logger.debug("No vector index on :%s(%s) to drop", label, prop)

    def create_fulltext_index(self, label: str, *props: str) -> None:
        """Ensure a FalkorDB full-text (BM25) index exists on :label(props) -- the lexical half
        of hybrid retrieval. Idempotent (an already-indexed attribute raises ResponseError,
        treated as success). Higher query score == more relevant."""
        try:
            self._graph.create_node_fulltext_index(label, *props)
            logger.debug("Created full-text index on :%s(%s)", label, ", ".join(props))
        except ResponseError as e:
            if "already" not in str(e).lower():  # a real failure, not the idempotent "already indexed"
                raise  # don't swallow it -- a missing index must not be reported as ready (await_indexes)
            logger.debug("Full-text index on :%s already exists", label)

    def await_indexes(self, label: str, timeout: float = _INDEX_BUILD_TIMEOUT_S) -> bool:
        """Block until every node index on :label is built: True once they all are, False if
        `timeout` seconds ran out first -- or if the label has no index at all (nothing is
        building, so nothing will become ready), or its state can't be read. FalkorDB returns
        from CREATE INDEX at once and populates the index in the background; until `db.indexes()`
        reports it OPERATIONAL, a vector or full-text query against it comes back EMPTY -- not
        partial (see _INDEX_BUILD_TIMEOUT_S)."""
        deadline = time.monotonic() + timeout
        pause = _INDEX_POLL_INTERVAL_S
        while True:
            try:
                rows = self._graph.ro_query(
                    "CALL db.indexes() YIELD label, entitytype, status "
                    "WHERE label = $label AND entitytype = 'NODE' RETURN status", {"label": label}
                ).result_set
            except RedisError as e:  # a server whose db.indexes() reports no status, a dropped connection
                logger.warning("Can't read the state of the indexes on :%s (%s)", label, e)
                return False
            if not rows:
                return False
            if all(status == "OPERATIONAL" for (status,) in rows):
                return True
            if time.monotonic() >= deadline:
                logger.warning("Indexes on :%s still building after %.0fs", label, timeout)
                return False
            time.sleep(max(0.0, min(pause, deadline - time.monotonic())))  # never past the deadline
            pause = min(pause * 2, _INDEX_POLL_MAX_INTERVAL_S)

    def _write(self, cypher: str, params: dict | None = None):
        """Every write funnels through here so it carries a generous per-query timeout
        (_WRITE_QUERY_TIMEOUT_MS) rather than inheriting FalkorDB's 1s server default, which a large
        batch easily exceeds -- the failure mode in #857, where a deep pass died mid-write with
        `Query timed out` and left a silently shallow graph."""
        return self._graph.query(cypher, params or {}, timeout=_WRITE_QUERY_TIMEOUT_MS)

    def _write_rows(self, cypher: str, rows: list, param: str = "rows") -> int:
        """Run an `UNWIND $<param> AS ...` batched write (param defaults to `rows`) and return the
        scalar it RETURNs summed across rows (0 when the query returns nothing -- e.g. a MERGE/DELETE
        with no RETURN). If the batch still times out despite the generous per-query timeout (a
        server pinned to a low TIMEOUT_MAX, or a pathologically dense batch), split it in half and
        retry each half, down to a single row, so the pass completes instead of failing (#857). A
        lone row that still times out is a genuine server problem and is re-raised."""
        try:
            result = self._write(cypher, {param: rows})
            return result.result_set[0][0] if result.result_set else 0
        except ResponseError as exc:
            if len(rows) <= 1 or "timed out" not in str(exc).lower():
                raise
            mid = len(rows) // 2
            logger.warning(
                "batched write of %d rows timed out; splitting into %d + %d and retrying (#857)",
                len(rows), mid, len(rows) - mid,
            )
            return (self._write_rows(cypher, rows[:mid], param)
                    + self._write_rows(cypher, rows[mid:], param))

    def query(self, cypher: str, params: dict | None = None):
        """Escape hatch for passes that need arbitrary Cypher (reads, or targeted
        deletes) beyond the structured merge/delta helpers -- e.g. a graph-enrichment
        ingestor resolving nodes and recomputing its own derived layer. Carries the same generous
        write timeout (a legitimately slow read or targeted delete shouldn't hit the 1s default
        either); a runaway query is still bounded by _WRITE_QUERY_TIMEOUT_MS."""
        return self._write(cypher, params)

    def ro_query(self, cypher: str, params: dict | None = None):
        """Read-only Cypher (rejected by FalkorDB if it contains a write clause)."""
        return self._graph.ro_query(cypher, params or {})

    def node_exists(self, label: str, node_id: str) -> bool:
        """Used by ingestors that link into another ingestor's nodes (e.g. PL/SQL
        attaching to the SQL ingestor's Table/Database nodes) to check the target
        graph already has what they need before writing dangling edges to it."""
        result = self._graph.query(f"MATCH (n:{label} {{id: $id}}) RETURN count(n)", {"id": node_id})
        return result.result_set[0][0] > 0

    def set_if(self, label: str, node_id: str, prop: str, value, guard_prop: str, guard_value) -> bool:
        """Atomically SET `n.<prop> = value` on the node with this id ONLY if `n.<guard_prop> ==
        guard_value`, returning whether the guard matched. One GRAPH.QUERY is atomic (FalkorDB/redis is
        single-threaded), so this is a compare-and-set for a single property. The two-phase deep pass
        stamps depth="deep" through here guarded on commit_sha: a concurrent ingest that moved the commit
        while the (background/async) deep pass ran leaves depth at "fast" instead of a stale deep claim --
        closing the read-then-decide TOCTOU in the depth short-circuit. `prop`/`guard_prop` are
        format-interpolated, so this is for trusted internal property names only, never user input."""
        result = self._write(
            f"MATCH (n:{label} {{id: $id}}) WHERE n.{guard_prop} = $gv SET n.{prop} = $v RETURN count(n)",
            {"id": node_id, "gv": guard_value, "v": value},
        )
        return bool(result.result_set and result.result_set[0][0])

    def merge_node(self, node: GraphNode) -> None:
        key_clause = ", ".join(f"{k}: ${k}" for k in node.key)
        params = dict(node.key)

        extra = {k: v for k, v in node.properties.items() if k not in node.key}
        extra["_fingerprint"] = fingerprint(node.properties)
        # SET the system label (n:Entity) alongside the props -- MERGE on the semantic label + key so
        # it still matches a pre-existing node, then add the shared label idempotently (see
        # SHARED_NODE_LABEL). Never MERGE on the multi-label pattern, which wouldn't match a node that
        # lacks the shared label and would fork a duplicate.
        set_clause = f" SET n:{SHARED_NODE_LABEL}, " + ", ".join(f"n.{k} = ${k}" for k in extra)
        params.update(extra)

        query = f"MERGE (n:{node.label} {{{key_clause}}}){set_clause}"
        self._write(query, params)

    def merge_edge(self, edge: GraphEdge) -> None:
        from_params = {f"from_{k}": v for k, v in edge.from_key.items()}
        to_params = {f"to_{k}": v for k, v in edge.to_key.items()}
        from_clause = ", ".join(f"{k}: $from_{k}" for k in edge.from_key)
        to_clause = ", ".join(f"{k}: $to_{k}" for k in edge.to_key)

        params: dict[str, object] = {**from_params, **to_params}
        extra = {**edge.properties, "_fingerprint": fingerprint(edge.properties)}
        set_clause = " SET " + ", ".join(f"r.{k} = ${k}" for k in extra)
        params.update(extra)

        query = (
            f"MATCH (a:{edge.from_label} {{{from_clause}}}), (b:{edge.to_label} {{{to_clause}}}) "
            f"MERGE (a)-[r:{edge.rel_type}]->(b){set_clause} "
            "RETURN count(a)"
        )
        result = self._write(query, params)
        if result.result_set[0][0] == 0:
            # MATCH found neither/either endpoint -- MERGE silently no-ops in that case
            # rather than erroring, which would otherwise hide bugs like a wrong label
            # (e.g. HAS_COLUMN emitted with from_label="Table" for what's really a View).
            logger.warning(
                "merge_edge found no matching endpoints for %s: (%s %s)-[:%s]->(%s %s)",
                edge.rel_type,
                edge.from_label,
                edge.from_key,
                edge.rel_type,
                edge.to_label,
                edge.to_key,
            )

    def merge_nodes(self, nodes: list[GraphNode]) -> None:
        """Batched equivalent of merge_node -- groups by label (Cypher can't
        parametrize a label) and issues one UNWIND-based MERGE per chunk instead of
        one query per node."""
        by_label: dict[str, list[GraphNode]] = defaultdict(list)
        for node in nodes:
            by_label[node.label].append(node)
        for label, group in by_label.items():
            self._merge_node_chunk(label, group)

    def _merge_node_chunk(self, label: str, nodes: list[GraphNode]) -> None:
        key_cols = list(nodes[0].key)
        key_clause = ", ".join(f"{k}: row.key.{k}" for k in key_cols)
        intern = _INTERN_NODE_PROPS if _interning_enabled() else frozenset()
        for chunk in _chunked(nodes, _WRITE_BATCH_SIZE):
            rows = []
            for n in chunk:
                props = {k: v for k, v in n.properties.items() if k not in n.key}
                props["_fingerprint"] = fingerprint(n.properties)
                # Interned props go under `iv` and are set once via intern() (never stored plain
                # -- that is what actually dedupes); everything else goes straight into `n += plain`.
                rows.append({
                    "key": dict(n.key),
                    "plain": {k: v for k, v in props.items() if k not in intern},
                    "iv": {k: v for k, v in props.items() if k in intern},
                })
            query = (
                f"UNWIND $rows AS row MERGE (n:{label} {{{key_clause}}}) "
                f"SET n:{SHARED_NODE_LABEL}, n += row.plain{_intern_clause('n', rows)}"
            )
            self._write_rows(query, rows)

    def merge_edges(self, edges: list[GraphEdge]) -> None:
        """Batched equivalent of merge_edge -- groups by (rel_type, from_label,
        to_label) and issues one UNWIND-based MERGE per chunk instead of one query
        per edge. Rows whose endpoints don't both exist are silently skipped by the
        MATCH (same as merge_edge's per-edge no-op), but -- unlike merge_edge --
        the mismatch is only detected in aggregate (a chunk-level count), so this
        can't name which specific edge was missing an endpoint."""
        by_group: dict[tuple[str, str, str], list[GraphEdge]] = defaultdict(list)
        for edge in edges:
            by_group[(edge.rel_type, edge.from_label, edge.to_label)].append(edge)
        for (rel_type, from_label, to_label), group in by_group.items():
            self._merge_edge_chunk(rel_type, from_label, to_label, group)

    def _merge_edge_chunk(self, rel_type: str, from_label: str, to_label: str, edges: list[GraphEdge]) -> None:
        from_cols = list(edges[0].from_key)
        to_cols = list(edges[0].to_key)
        from_clause = ", ".join(f"{k}: row.from_key.{k}" for k in from_cols)
        to_clause = ", ".join(f"{k}: row.to_key.{k}" for k in to_cols)
        intern = _INTERN_EDGE_PROPS if _interning_enabled() else frozenset()
        for chunk in _chunked(edges, _WRITE_BATCH_SIZE):
            rows = []
            for e in chunk:
                props = {**e.properties, "_fingerprint": fingerprint(e.properties)}
                rows.append({
                    "from_key": dict(e.from_key),
                    "to_key": dict(e.to_key),
                    "plain": {k: v for k, v in props.items() if k not in intern},
                    "iv": {k: v for k, v in props.items() if k in intern},
                })
            query = (
                f"UNWIND $rows AS row "
                f"MATCH (a:{from_label} {{{from_clause}}}), (b:{to_label} {{{to_clause}}}) "
                f"MERGE (a)-[r:{rel_type}]->(b) SET r += row.plain{_intern_clause('r', rows)} "
                "RETURN count(r)"
            )
            matched = self._write_rows(query, rows)
            if matched < len(chunk):
                logger.warning(
                    "merge_edges: %d of %d %s edges (%s -> %s) had a missing endpoint and were skipped",
                    len(chunk) - matched,
                    len(chunk),
                    rel_type,
                    from_label,
                    to_label,
                )

    def get_property(self, label: str, key: dict, prop_name: str) -> object | None:
        """Single-node, single-property lookup -- e.g. checking a Project node's
        current `commit_sha` before deciding whether a git sync has anything to do,
        without paying for a full read_fingerprints() just to answer that
        question."""
        key_clause = ", ".join(f"{k}: ${k}" for k in key)
        result = self._graph.ro_query(f"MATCH (n:{label} {{{key_clause}}}) RETURN n.{prop_name}", dict(key))
        if not result.result_set:
            return None
        return result.result_set[0][0]

    def delete_node(self, label: str, key: dict) -> None:
        key_clause = ", ".join(f"{k}: ${k}" for k in key)
        self._write(f"MATCH (n:{label} {{{key_clause}}}) DETACH DELETE n", dict(key))

    def delete_edge(self, rel_type: str, from_label: str, from_key: dict, to_label: str, to_key: dict) -> None:
        from_params = {f"from_{k}": v for k, v in from_key.items()}
        to_params = {f"to_{k}": v for k, v in to_key.items()}
        from_clause = ", ".join(f"{k}: $from_{k}" for k in from_key)
        to_clause = ", ".join(f"{k}: $to_{k}" for k in to_key)
        query = f"MATCH (a:{from_label} {{{from_clause}}})-[r:{rel_type}]->(b:{to_label} {{{to_clause}}}) DELETE r"
        self._write(query, {**from_params, **to_params})

    def delete_nodes(self, items: list[tuple[str, dict]]) -> None:
        """Batched equivalent of delete_node -- groups by label, one UNWIND-based
        DETACH DELETE per chunk instead of one query per node."""
        by_label: dict[str, list[dict]] = defaultdict(list)
        for label, key in items:
            by_label[label].append(key)
        for label, keys in by_label.items():
            key_cols = list(keys[0])
            key_clause = ", ".join(f"{k}: row.{k}" for k in key_cols)
            for chunk in _chunked(keys, _WRITE_BATCH_SIZE):
                query = f"UNWIND $rows AS row MATCH (n:{label} {{{key_clause}}}) DETACH DELETE n"
                self._write_rows(query, chunk)

    def delete_edges(self, items: list[tuple[str, str, dict, str, dict]]) -> None:
        """Batched equivalent of delete_edge -- groups by (rel_type, from_label,
        to_label), one UNWIND-based DELETE per chunk instead of one query per edge."""
        by_group: dict[tuple[str, str, str], list[tuple[dict, dict]]] = defaultdict(list)
        for rel_type, from_label, from_key, to_label, to_key in items:
            by_group[(rel_type, from_label, to_label)].append((from_key, to_key))
        for (rel_type, from_label, to_label), pairs in by_group.items():
            from_cols = list(pairs[0][0])
            to_cols = list(pairs[0][1])
            from_clause = ", ".join(f"{k}: row.from_key.{k}" for k in from_cols)
            to_clause = ", ".join(f"{k}: row.to_key.{k}" for k in to_cols)
            for chunk in _chunked(pairs, _WRITE_BATCH_SIZE):
                rows = [{"from_key": dict(from_key), "to_key": dict(to_key)} for from_key, to_key in chunk]
                query = (
                    f"UNWIND $rows AS row "
                    f"MATCH (a:{from_label} {{{from_clause}}})-[r:{rel_type}]->(b:{to_label} {{{to_clause}}}) "
                    "DELETE r"
                )
                self._write_rows(query, rows)

    def read_fingerprints(self, id_prefix: str, node_labels: list[str]) -> tuple[dict[NodeKey, str], dict[EdgeKey, str]]:
        """The live graph's current (key -> fingerprint) for every node/edge (both
        endpoints, for edges) that `id_prefix` owns -- the id itself and every id that
        continues it at a `/` or `.` boundary, never a sibling whose name merely starts
        with it (see _owned_ids) -- the lightweight
        current-state counterpart to a freshly computed target state, for
        graph_sync.compute_delta(). Fetches only `id` and the stored `_fingerprint`
        (see graph_sync.fingerprint()), not full properties -- a node/edge whose
        content hasn't changed since the last sync never needs its full properties
        transferred just to confirm that. Relies on the convention (true for every
        id this project's ingestors emit) that a project's own ids are always
        prefixed by that project's id, and that ingestors don't create edges
        spanning two different projects' nodes.

        `node_labels` (a project's own NODE_SCHEMAS labels) is required, not
        discovered, because of how this is queried -- see _SUBGRAPH_QUERY_TIMEOUT_MS.

        Issues one query per (label, node-or-edge) pair concurrently -- these
        reads are all independent and nothing writes to the graph in between, so
        there's no correctness reason to serialize them (see
        _SUBGRAPH_READ_WORKERS)."""
        if not node_labels:
            return {}, {}
        workers = min(len(node_labels) * 2, _SUBGRAPH_READ_WORKERS)
        node_fingerprints: dict[NodeKey, str] = {}
        edge_fingerprints: dict[EdgeKey, str] = {}
        with phase("read_fingerprints"), ThreadPoolExecutor(max_workers=workers) as pool:
            node_futures = [pool.submit(self._read_node_fingerprints, label, id_prefix) for label in node_labels]
            edge_futures = [pool.submit(self._read_edge_fingerprints, label, id_prefix) for label in node_labels]
            for future in node_futures:
                node_fingerprints.update(future.result())
            for future in edge_futures:
                edge_fingerprints.update(future.result())
            theirs = self._nested_projects(id_prefix)  # after every read has its rows -- see there
        if theirs:
            node_fingerprints = {
                k: fp for k, fp in node_fingerprints.items() if not _owned_by(dict(k[1])["id"], theirs)
            }
            edge_fingerprints = {
                k: fp for k, fp in edge_fingerprints.items()
                if not (_owned_by(dict(k[2])["id"], theirs) or _owned_by(dict(k[4])["id"], theirs))
            }
        return node_fingerprints, edge_fingerprints

    def _nested_projects(self, id_prefix: str) -> tuple[str, ...]:
        """Ids of the OTHER projects whose name continues `id_prefix` at a boundary --
        `Acme.Core.Tests` next to `Acme.Core`, or `csharp://Sol/Core` (a project in solution `Sol`)
        next to a project that is itself named `Sol`. Every id of such a project matches
        _owned_ids, shaped just like this project's own `<proj>.class.x` / `<proj>/file/...`; only
        its Project node tells the two apart. What it owns is left out of the reads (so never
        diffed away as this project's leftovers) -- where an id could be either's, it stays.

        read_fingerprints looks this up once, AFTER every read has its rows: a language ingestor's
        sync writes its Project node before its other nodes, so a project another session creates in
        the meantime is already visible here by the time any of its rows could have been read. The
        compose, k8s, terraform, helm, kustomize and ci ingestors write it last, so a brand-new
        project of theirs can still have its first batch read as leftovers."""
        owned = _owned_ids(id_prefix)
        rows = self._graph.ro_query(
            "MATCH (p:Project) WHERE p.id STARTS WITH $sub OR p.id STARTS WITH $dot RETURN p.id",
            {"sub": owned["sub"], "dot": owned["dot"]},
        ).result_set
        return tuple(row[0] for row in rows)

    def _read_node_fingerprints(self, label: str, id_prefix: str) -> dict[NodeKey, str]:
        rows = self._graph.ro_query(
            _node_fingerprint_query(label), _owned_ids(id_prefix), timeout=_SUBGRAPH_QUERY_TIMEOUT_MS
        ).result_set
        return {node_key(GraphNode(label, {"id": node_id}, {})): fp for node_id, fp in rows}

    def _read_edge_fingerprints(self, from_label: str, id_prefix: str) -> dict[EdgeKey, str]:
        """Every outgoing edge's key -> fingerprint from a node labeled `from_label`
        whose id has this project's prefix -- iterating over every one of the
        project's own labels as `from_label` in read_fingerprints() above therefore
        covers every edge the project owns exactly once, since every node this
        project ever creates has exactly one of those labels."""
        rows = self._graph.ro_query(
            _edge_fingerprint_query(from_label),
            _owned_ids(id_prefix),
            timeout=_SUBGRAPH_QUERY_TIMEOUT_MS,
        ).result_set
        return {
            edge_key(GraphEdge(rel_type, from_label, {"id": from_id}, to_label, {"id": to_id}, {})): fp
            for rel_type, to_label, from_id, to_id, fp in rows
        }

    def apply_delta(self, delta: GraphDelta) -> None:
        # The node carrying SYNC_MARKER is what the next sync short-circuits on (see
        # graph_sync.SYNC_MARKER): its stamp is cleared before anything else is written, and the
        # node is merged whole -- stamp included -- only once the rest of the delta is applied.
        markers = [n for n in delta.nodes_to_upsert if SYNC_MARKER in n.properties]
        with phase("write"):
            if markers:
                self.merge_nodes([unstamped(n) for n in markers])
            if delta.nodes_to_upsert:
                self.merge_nodes([n for n in delta.nodes_to_upsert if SYNC_MARKER not in n.properties])
                # Content-changed nodes keep their now-stale embedding through the merge (it isn't
                # in the ingestor's property set) -- clear it so the worker re-embeds. New nodes
                # in this set have no embedding, so clearing them is a no-op.
                self.invalidate_embeddings((n.label, n.key["id"]) for n in delta.nodes_to_upsert)
            if delta.edges_to_upsert:
                self.merge_edges(delta.edges_to_upsert)
            if delta.edges_to_remove:
                self.delete_edges(delta.edges_to_remove)
            if delta.nodes_to_remove:
                self.delete_nodes(delta.nodes_to_remove)
            if markers:
                self.merge_nodes(markers)

    # -- embeddings (semantic search) -------------------------------------------------------
    #
    # An embedded node carries the shared `embedding` vector property plus a shared marker label
    # (default `Embeddable`) so ONE vector index + ONE KNN query span every node kind
    # (Function/Method/Class/File/...). The presence of the `embedding` property is itself the
    # "already embedded?" state -- no per-node model/dim/hash bookkeeping (see
    # core/embeddings/embedder.py). vectors are written as FalkorDB `vecf32(...)` values.

    SHARED_EMBEDDING_LABEL = "Embeddable"
    EMBEDDING_PROPERTY = "embedding"
    FULLTEXT_PROPERTIES = ("name", "doc")  # fields the lexical (full-text) ranker indexes

    def write_embeddings(self, items: list[tuple[str, str, list[float], str | None]]) -> None:
        """Attach vectors to existing nodes. `items` is (kind_label, node_id, vector, fingerprint)
        tuples; each node is matched by its own kind label + id (index-backed) and gains the shared
        marker label + `embedding` property. Grouped by kind label and batched (Cypher can't
        parametrize a label), one UNWIND write per chunk.

        The write is **guarded by the content fingerprint** observed when the document was built:
        `coalesce(n._fingerprint,'') = row.fp`. If the node's content changed in between -- e.g. a
        concurrent resync re-merged it (new `_fingerprint`) and cleared its embedding -- the match
        fails and the now-stale vector is dropped rather than marking the node done, so it stays
        pending for the next pass. (Nodes written without a fingerprint, e.g. in tests, compare
        '' = '' and still take the vector.)"""
        by_label: dict[str, list[tuple[str, list[float], str | None]]] = defaultdict(list)
        for kind_label, node_id, vector, fingerprint in items:
            by_label[kind_label].append((node_id, vector, fingerprint))
        for kind_label, group in by_label.items():
            for chunk in _chunked(group, _WRITE_BATCH_SIZE):
                rows = [{"id": nid, "vec": list(vec), "fp": fp or ""} for nid, vec, fp in chunk]
                query = (
                    f"UNWIND $rows AS row MATCH (n:{kind_label} {{id: row.id}}) "
                    "WHERE coalesce(n._fingerprint, '') = row.fp "
                    f"SET n:{self.SHARED_EMBEDDING_LABEL}, n.{self.EMBEDDING_PROPERTY} = vecf32(row.vec)"
                )
                self._write_rows(query, rows)

    def clear_embeddings(self, items: list[tuple[str, str]]) -> None:
        """Remove the vector + marker label from nodes so the embed worker re-embeds them -- used
        when an incremental resync changes a node's content. `items` is (kind_label, node_id)
        pairs. Grouped by label + batched."""
        by_label: dict[str, list[str]] = defaultdict(list)
        for kind_label, node_id in items:
            by_label[kind_label].append(node_id)
        for kind_label, ids in by_label.items():
            for chunk in _chunked(ids, _WRITE_BATCH_SIZE):
                query = (
                    f"UNWIND $ids AS id MATCH (n:{kind_label} {{id: id}}) "
                    f"REMOVE n:{self.SHARED_EMBEDDING_LABEL}, n.{self.EMBEDDING_PROPERTY}"
                )
                self._write_rows(query, chunk, param="ids")

    def clear_all_embeddings(self) -> None:
        """Remove every vector + the shared marker label from the graph -- the data half of a model
        switch (pair with drop_vector_index, which removes the index half). The embed worker then
        recomputes every node from scratch under the new model's dimension."""
        self._write(
            f"MATCH (n:{self.SHARED_EMBEDDING_LABEL}) "
            f"REMOVE n:{self.SHARED_EMBEDDING_LABEL}, n.{self.EMBEDDING_PROPERTY}"
        )

    def invalidate_embeddings(self, items) -> None:
        """Clear embeddings for the embeddable nodes among `items` ((kind_label, node_id) pairs).
        Called by the sync paths when a node's content (`source`/`doc`) changed: merging the new
        properties leaves the worker-written `embedding` in place (it isn't in the ingestor's
        property set), so it would go stale -- clearing it re-queues the node for the embed worker.
        Non-embeddable labels are skipped; a node with no embedding is a harmless no-op."""
        from auryn.core.embeddings.documents import EMBEDDABLE_LABELS

        pairs = [(label, node_id) for label, node_id in items if label in EMBEDDABLE_LABELS]
        if pairs:
            self.clear_embeddings(pairs)

    def knn_search(
        self, query_vec: list[float], k: int, *, return_properties: tuple[str, ...] = ()
    ) -> list[dict]:
        """K nearest embedded nodes to `query_vec`, nearest first. Returns one dict per hit with
        `id`, `kind` (the node's real label, not the shared marker), `score` (cosine distance --
        smaller is closer), and any `return_properties` projected from the node. Read-only; returns
        [] cleanly if no vector index exists yet (embeddings still building)."""
        proj = "".join(f", n.{p} AS {p}" for p in return_properties)
        query = (
            f"CALL db.idx.vector.queryNodes('{self.SHARED_EMBEDDING_LABEL}', "
            f"'{self.EMBEDDING_PROPERTY}', $k, vecf32($q)) YIELD node AS n, score "
            f"RETURN n.id AS id, {semantic_label('n')} AS kind, "
            f"score{proj} ORDER BY score"
        )
        try:
            result = self._graph.ro_query(query, {"k": k, "q": list(query_vec)})
        except ResponseError:
            return []  # no vector index yet -> semantic search unavailable, degrade to empty
        columns = [name for _, name in result.header]
        return [dict(zip(columns, row)) for row in result.result_set]

    def fulltext_search(self, query: str, k: int, *, return_properties: tuple[str, ...] = ()) -> list[dict]:
        """Top-k embedded nodes by lexical (BM25) relevance to `query` -- the keyword half of
        hybrid retrieval. The query is reduced to content tokens (see `_fulltext_tokens`) and
        OR-joined, so a natural-language phrase matches on any term (FalkorDB's default AND would
        over-filter). Same result shape as `knn_search` (`id`, `kind`, `score`, projected props),
        best-first (`score` is relevance -- higher is better, opposite of the vector distance).
        Returns [] on an empty token set or when no full-text index exists yet."""
        tokens = _fulltext_tokens(query)
        if not tokens:
            return []
        ft_query = "|".join(tokens)  # OR semantics for recall; tokens are sanitized to be operator-safe
        proj = "".join(f", n.{p} AS {p}" for p in return_properties)
        cypher = (
            f"CALL db.idx.fulltext.queryNodes('{self.SHARED_EMBEDDING_LABEL}', $q) YIELD node AS n, score "
            f"RETURN n.id AS id, {semantic_label('n')} AS kind, "
            f"score{proj} ORDER BY score DESC LIMIT $k"
        )
        try:
            result = self._graph.ro_query(cypher, {"q": ft_query, "k": k})
        except ResponseError:
            return []  # no full-text index yet -> lexical search unavailable, degrade to empty
        columns = [name for _, name in result.header]
        return [dict(zip(columns, row)) for row in result.result_set]
