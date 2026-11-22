"""ORM model<->table stitch.

A SQLAlchemy model is a Python class that names a physical table (`__tablename__ = "users"`) and maps
its columns (`id = Column(Integer, ...)` / `name: Mapped[str] = mapped_column(...)` / an annotation-only
`email: Mapped[str]`). A SQL ingestor (sql / oracle / plsql) produces the physical `Table`/`Column`
nodes from the database schema. This pass joins the two across the shared graph -- by table/column NAME,
never by symbol -- so the golden path reads:

    function -CALLS-> ... -> Model class -MAPS_TO-> Table <-HAS_COLUMN- Column
                                         -MAPS_TO-> Column

i.e. a query can trace application code to the physical table/column it reads or writes -- the "project
A (app) to project B (database)" connection. Language/driver-agnostic: it reads each class's stored
`source` from the graph (no file I/O, no Python-ingestor change) and matches names against whatever SQL
ingestor populated the schema. `trace_entrypoint` follows these Table edges from the models in its call
closure and lists them as `tables_mapped` (see core/graph_queries.py).

Name matching is per-schema-scope, exact-case-first then case-folded: a producer folds an unquoted name
to its own case (Oracle UPPER, PostgreSQL lower, SQLite as-written), so within one schema a model's
`__tablename__ = "users"` reaches a folded `USERS`, yet a producer holding both `users` and a quoted
`"Users"` links only the exact-case one. Matches are then unioned across scopes, so the same model still
maps to the `users` in EVERY producer that has one (the 1-to-many across dialects). See `_lookup` /
`_lookup_scoped`. Built once as an in-memory index (two graph reads) so the pass is O(models) writes,
not O(models x columns) round-trips.

Idempotent + order-independent: it owns its layer (the `orm-stitch` MAPS_TO edges), rebuilt from the
current graph each run, so code-first vs schema-first ingestion converge and a re-ingest self-corrects.
1-to-many is deliberate -- a model whose table name exists in two schemas links both.
"""

from __future__ import annotations

import ast
import logging

from auryn.core.graph_client import GraphWriter
from auryn.core.graph_model import GraphEdge

logger = logging.getLogger("auryn.orm")

# The constructors that mark a class attribute as a mapped column (classic + SQLAlchemy 2.0).
_COLUMN_FUNCS = frozenset({"Column", "mapped_column"})


def run(writer: GraphWriter, repo, target_sha: str) -> dict | None:
    """GraphEnrichment entrypoint. `repo`/`target_sha` are unused -- the stitch reads only the finished
    graph. Returns None when there's nothing to join (no SQL tables, or no model maps to one)."""
    return stitch(writer)


def stitch(writer: GraphWriter) -> dict | None:
    # Need physical tables to join to; this also guards the empty-graph "operation on empty key".
    try:
        if not writer.ro_query("MATCH (t:Table) RETURN t.id LIMIT 1").result_set:
            return None
    except Exception:
        return None

    _clear_prior_layer(writer)

    # In-memory name indexes (two reads for the whole schema -> O(1) per lookup, no per-model query),
    # each kept in BOTH exact and lowered forms. Tables are indexed PER SCHEMA SCOPE (its producer -- the
    # Schema/Project that HAS_TABLE it): case semantics are a per-dialect property, so matching is decided
    # within one producer, by that producer's dialect (see `_dialect_fold`/`_match`). Across scopes the
    # matches are unioned (`_lookup_scoped`), so a model `users` still maps to BOTH an Oracle `USERS` and a
    # PostgreSQL `users` (the 1-to-many across dialects), while a PostgreSQL scope holding only a quoted
    # `"Users"` does NOT falsely match a lowercase `users` model (and `users`+`"Users"` links only `users`).
    table_scopes: dict[str, tuple[dict[str, list[str]], dict[str, list[str]]]] = {}
    for tid, tname, scope in writer.ro_query(
        "MATCH (t:Table) OPTIONAL MATCH (p)-[:HAS_TABLE]->(t) "
        "WITH t, head(collect(p.id)) AS scope RETURN t.id, t.name, scope"
    ).result_set:
        if tname is not None:
            tid, tname = str(tid), str(tname)
            exact, lower = table_scopes.setdefault("" if scope is None else str(scope), ({}, {}))
            exact.setdefault(tname, []).append(tid)
            lower.setdefault(tname.lower(), []).append(tid)
    cols_exact: dict[str, dict[str, list[str]]] = {}
    cols_lower: dict[str, dict[str, list[str]]] = {}
    for tid, cid, cname in writer.ro_query(
        "MATCH (t:Table)-[:HAS_COLUMN]->(c:Column) RETURN t.id, c.id, c.name"
    ).result_set:
        if cname is not None:
            tid, cid, cname = str(tid), str(cid), str(cname)
            cols_exact.setdefault(tid, {}).setdefault(cname, []).append(cid)
            cols_lower.setdefault(tid, {}).setdefault(cname.lower(), []).append(cid)

    classes = writer.ro_query(
        "MATCH (c:Class) WHERE c.id STARTS WITH 'python://' AND c.source IS NOT NULL "
        "RETURN c.id, c.source"
    ).result_set

    edges: list[GraphEdge] = []
    models = 0
    for cid, source in classes:
        parsed = _parse_model(str(source))
        if parsed is None:
            continue
        tablename, columns = parsed
        table_ids = _lookup_scoped(tablename, table_scopes)
        if not table_ids:
            continue  # the model names a table no ingested schema has (yet) -- nothing to join
        models += 1
        for tid in table_ids:  # 1-to-many: the same table name in >1 schema -> link each
            edges.append(GraphEdge("MAPS_TO", "Class", {"id": str(cid)}, "Table", {"id": tid},
                                   {"provenance": "orm-stitch", "table": tablename}))
            col_fold = _dialect_fold(tid)  # the owning table's dialect governs its columns' case too
            for attr, col in columns:
                for colid in _match(col, cols_exact.get(tid, {}), cols_lower.get(tid, {}), col_fold):
                    edges.append(GraphEdge("MAPS_TO", "Class", {"id": str(cid)}, "Column", {"id": colid},
                                           {"provenance": "orm-stitch", "attribute": attr, "column": col}))

    if not models:
        return None
    writer.merge_edges(edges)  # one batched write, not one round-trip per edge
    table_edges = sum(1 for e in edges if e.to_label == "Table")
    logger.info("ORM stitch: %d model(s) -> %d table + %d column MAPS_TO edge(s)",
                models, table_edges, len(edges) - table_edges)
    return {"models": models, "table_edges": table_edges, "column_edges": len(edges) - table_edges}


def _dialect_fold(id_or_scope: str):
    """How the scope's dialect case-folds an UNQUOTED identifier, inferred from the id scheme
    (`oracle://`, `postgresql://`, ...): `str.upper` for Oracle, `str.lower` for PostgreSQL, else None.
    None means "case-insensitive / unknown" (SQLite, MySQL, MSSQL, the generic `sql://`, source-derived
    PL/SQL) -- then `_match` falls back to a lenient case-insensitive lookup."""
    scheme = id_or_scope.split("://", 1)[0].lower() if "://" in id_or_scope else ""
    if scheme == "oracle":
        return str.upper
    if scheme in ("postgresql", "postgres"):
        return str.lower
    return None


def _match(name: str, exact: dict[str, list[str]], lower: dict[str, list[str]], fold) -> list[str]:
    """Resolve one model name within one scope (a schema's tables, or a table's columns). An exact-case
    hit always wins (so a quoted/mixed-case identifier matches verbatim). When there's no exact hit:
    - a dialect with a known unquoted-folding rule (`fold`) folds the MODEL name and exact-matches that
      (`users`->`USERS` reaches Oracle; `users`->`users` does NOT reach a PostgreSQL quoted `"Users"`),
    - an unknown/case-insensitive dialect (`fold is None`) falls back to a lenient lowercased lookup.
    This keeps case-distinct identifiers in one producer apart (`users` vs `"Users"`, `id` vs `"ID"`)
    while still bridging a lowercase model to an Oracle UPPER schema."""
    hits = exact.get(name)
    if hits:
        return hits
    if fold is not None:
        return exact.get(fold(name), [])
    return lower.get(name.lower(), [])


def _lookup_scoped(name: str, scopes: dict[str, tuple[dict[str, list[str]], dict[str, list[str]]]]) -> list[str]:
    """Resolve a table name across every schema scope and union the per-scope matches, each scope matched
    under its own dialect (`_dialect_fold`). So a model maps to the matching table in EACH producer that
    has it -- `users` reaches both an Oracle `USERS` and a PostgreSQL `users` (the documented 1-to-many
    across dialects) -- while within a single producer the per-dialect rule prevents case-distinct
    over-matches."""
    out: list[str] = []
    for scope, (exact, lower) in scopes.items():
        out.extend(_match(name, exact, lower, _dialect_fold(scope)))
    return out


def _clear_prior_layer(writer: GraphWriter) -> None:
    """Delete this hook's own prior MAPS_TO edges so a re-run rebuilds its layer from scratch
    (idempotent + order-independent -- mirrors the http stitch owning its EXPOSES_ENDPOINT edges).
    Only `orm-stitch`-provenanced edges are touched, never another layer's."""
    writer.query("MATCH (:Class)-[r:MAPS_TO]->() WHERE r.provenance = 'orm-stitch' DELETE r")


def _parse_model(source: str) -> tuple[str, list[tuple[str, str]]] | None:
    """Detect a SQLAlchemy model from a class's stored `source`. Returns (table_name, [(attr, column)])
    or None if it isn't one. A model is recognized by a `__tablename__ = "..."` assignment (the reliable
    declarative marker). Columns are class-body attributes that are either assigned `Column(...)` /
    `mapped_column(...)`, OR annotation-only `x: Mapped[...]` (SQLAlchemy 2.0 infers a physical column
    for these). The column name is the constructor's first positional string / its `name=` keyword when
    given, else the attribute name."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    cls = next((n for n in tree.body if isinstance(n, ast.ClassDef)), None)
    if cls is None:
        return None

    tablename: str | None = None
    columns: list[tuple[str, str]] = []
    for stmt in cls.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            attr, value, annotation = stmt.targets[0].id, stmt.value, None
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            attr, value, annotation = stmt.target.id, stmt.value, stmt.annotation
        else:
            continue

        if attr == "__tablename__" and isinstance(value, ast.Constant) and isinstance(value.value, str):
            tablename = value.value
        elif isinstance(value, ast.Call) and _call_name(value.func) in _COLUMN_FUNCS:
            columns.append((attr, _column_name(value) or attr))
        elif value is None and _is_mapped_annotation(annotation):
            columns.append((attr, attr))  # `email: Mapped[str]` -- SQLAlchemy 2.0 infers the column

    if tablename is None:
        return None
    return tablename, columns


def _call_name(node) -> str | None:
    """The simple name of a call target / subscript base: `Column(...)`/`sa.Column(...)` -> "Column",
    `Mapped[int]` base -> "Mapped"."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_mapped_annotation(annotation) -> bool:
    """True for a `Mapped[...]` annotation (SQLAlchemy 2.0), so an annotation-only mapped attribute is a
    column -- while an ordinary class annotation (`x: int`) is not."""
    return isinstance(annotation, ast.Subscript) and _call_name(annotation.value) == "Mapped"


def _column_name(call: ast.Call) -> str | None:
    """The explicit physical column name from a `Column(...)`/`mapped_column(...)` call: the first
    POSITIONAL string literal (`Column("email_address", String)`) or a `name="..."` keyword
    (`mapped_column(String, name="email_address")`). None when neither is given (use the attribute name).
    A type-first call like `Column(String)` has no string name -> None."""
    if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
        return call.args[0].value
    for kw in call.keywords:
        if kw.arg == "name" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value
    return None
