"""Turn parsed object-DDL files (raw_model.RawPlsqlFile) into the graph nodes/edges
for ONE schema, rooted under its `oracle://<db>.<SCHEMA>` id.

This is the file-based counterpart to the live `sql` ingestor and it mirrors that
ingestor's ids and properties exactly -- Table `oracle://db.SCHEMA.TABLE`, Column
`...TABLE.COLUMN`, Index `...TABLE.index.NAME`, Constraint `...TABLE.constraint.NAME`,
Sequence `...SCHEMA.sequence.NAME`, View `...SCHEMA.VIEW` -- so a schema reconstructed
from files and the same schema reflected from a live connection are the same subgraph,
not two shadows of it. Identifiers are UPPERCASE (the parser upper-cases every name,
matching Oracle's catalog), which is what makes those ids line up with a live reflect.

Emission is two-pass for the same reason the other file ingestors are: BaseIngestor's
batch writer only guarantees nodes-before-edges *within* a 2000-item batch, so every
node (and the HAS_* edge to a parent already emitted) is yielded first, and the
cross-cutting edges whose endpoints can live in different tables (foreign-key
REFERENCES, view READS) come in a second pass once every endpoint node exists.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.ingestors.plsql.raw_model import (
    RawIndexDef,
    RawPlsqlFile,
    RawSequence,
    RawTable,
    RawView,
)


def resolve_schema(raw_files: Iterable[RawPlsqlFile], schema_id: str) -> Iterator[GraphNode | GraphEdge]:
    """Yield every Table/Column/Constraint/Index/Sequence/View node and their edges for
    one schema (the Database/Schema roots and HAS_SCHEMA are emitted by the caller).
    `schema_id` is `oracle://<db>.<SCHEMA>`."""
    files = list(raw_files)
    tables = _dedupe_tables(t for f in files for t in f.tables)
    _apply_alterations(tables, [a for f in files for a in f.alterations])
    views = _dedupe_by_name(v for f in files for v in f.views)
    indexes = [i for f in files for i in f.indexes]
    sequences = _dedupe_by_name(s for f in files for s in f.sequences)

    defined = {t.name.upper() for t in tables}
    # A table referenced by a FK or a view but never CREATEd in this export still needs a
    # node so the REFERENCES/READS/HAS_INDEX edge has a landing point (mirrors the live
    # reflect, where such a table would simply exist). Emitted as a reference-only stub.
    referenced = _referenced_tables(tables, views, indexes)
    stub_names = {n for n in referenced if n.upper() not in defined}

    # Pass 1 -- nodes + HAS_* edges to an already-emitted parent.
    for table in tables:
        yield from _emit_table(table, schema_id)
    for name in sorted(stub_names):
        yield from _emit_table_stub(name, schema_id)
    for view in views:
        yield from _emit_view(view, schema_id)
    for sequence in sequences:
        yield from _emit_sequence(sequence, schema_id)
    for index in indexes:
        yield from _emit_index(index, schema_id)
    for table in tables:
        yield from _emit_constraints(table, schema_id)

    # Pass 2 -- cross-cutting edges, once every endpoint node above exists.
    for table in tables:
        yield from _emit_foreign_keys(table, schema_id)
    for view in views:
        yield from _emit_view_reads(view, schema_id)


# -- Ids ----------------------------------------------------------------------------
# These f-strings match auryn.ingestors.sql.ingestor byte-for-byte -- that identity
# is the whole point (file-reconstructed and live-reflected schemas share node ids).


def _table_id(schema_id: str, name: str) -> str:
    return f"{schema_id}.{name.upper()}"


def _column_id(table_id: str, name: str) -> str:
    return f"{table_id}.{name.upper()}"


def _view_id(schema_id: str, name: str) -> str:
    return f"{schema_id}.{name.upper()}"


def _index_id(table_id: str, name: str) -> str:
    return f"{table_id}.index.{name.upper()}"


def _constraint_id(table_id: str, name: str) -> str:
    return f"{table_id}.constraint.{name.upper()}"


def _sequence_id(schema_id: str, name: str) -> str:
    return f"{schema_id}.sequence.{name.upper()}"


# -- Emitters -----------------------------------------------------------------------


def _emit_table(table: RawTable, schema_id: str) -> Iterator[GraphNode | GraphEdge]:
    table_id = _table_id(schema_id, table.name)
    yield GraphNode(
        "Table",
        {"id": table_id},
        {"name": table.name.upper(), "comment": None, "provenance": "source"},
    )
    yield GraphEdge("HAS_TABLE", "Schema", {"id": schema_id}, "Table", {"id": table_id})
    for column in table.columns:
        column_id = _column_id(table_id, column.name)
        yield GraphNode(
            "Column",
            {"id": column_id},
            {
                "name": column.name.upper(),
                "ordinal_position": column.position,
                "data_type": column.data_type,
                "nullable": column.nullable,
                "default_value": column.default_value,
                "is_primary_key": column.is_primary_key,
                "is_autoincrement": False,  # Oracle uses sequences/identity, not a reflectable flag here
                "comment": None,
                "provenance": "source",
            },
        )
        yield GraphEdge("HAS_COLUMN", "Table", {"id": table_id}, "Column", {"id": column_id})


def _emit_table_stub(name: str, schema_id: str) -> Iterator[GraphNode | GraphEdge]:
    table_id = _table_id(schema_id, name)
    yield GraphNode(
        "Table",
        {"id": table_id},
        {"name": name.upper(), "comment": None, "provenance": "source", "reference_only": True},
    )
    yield GraphEdge("HAS_TABLE", "Schema", {"id": schema_id}, "Table", {"id": table_id})


def _emit_constraints(table: RawTable, schema_id: str) -> Iterator[GraphNode | GraphEdge]:
    table_id = _table_id(schema_id, table.name)
    for ordinal, constraint in enumerate(table.constraints):
        name = (constraint.name or f"{constraint.type.lower()}_{ordinal}").upper()
        constraint_id = _constraint_id(table_id, name)
        yield GraphNode(
            "Constraint",
            {"id": constraint_id},
            {
                "name": name,
                "type": constraint.type,
                "expression": constraint.expression,
                "provenance": "source",
            },
        )
        yield GraphEdge("HAS_CONSTRAINT", "Table", {"id": table_id}, "Constraint", {"id": constraint_id})
        for position, column in enumerate(constraint.columns, start=1):
            yield GraphEdge(
                "CONSTRAINS",
                "Constraint",
                {"id": constraint_id},
                "Column",
                {"id": _column_id(table_id, column)},
                {"position": position},
            )


def _emit_index(index: RawIndexDef, schema_id: str) -> Iterator[GraphNode | GraphEdge]:
    table_id = _table_id(schema_id, index.table)
    index_id = _index_id(table_id, index.name)
    yield GraphNode(
        "Index",
        {"id": index_id},
        {"name": index.name.upper(), "unique": index.unique, "provenance": "source"},
    )
    yield GraphEdge("HAS_INDEX", "Table", {"id": table_id}, "Index", {"id": index_id})
    for position, column in enumerate(index.columns, start=1):
        yield GraphEdge(
            "INDEXES",
            "Index",
            {"id": index_id},
            "Column",
            {"id": _column_id(table_id, column)},
            {"position": position},
        )


def _emit_sequence(sequence: RawSequence, schema_id: str) -> Iterator[GraphNode | GraphEdge]:
    sequence_id = _sequence_id(schema_id, sequence.name)
    yield GraphNode(
        "Sequence",
        {"id": sequence_id},
        {"name": sequence.name.upper(), "provenance": "source"},
    )
    yield GraphEdge("HAS_SEQUENCE", "Schema", {"id": schema_id}, "Sequence", {"id": sequence_id})


def _emit_view(view: RawView, schema_id: str) -> Iterator[GraphNode | GraphEdge]:
    view_id = _view_id(schema_id, view.name)
    yield GraphNode(
        "View",
        {"id": view_id},
        {
            "name": view.name.upper(),
            "kind": "VIEW",
            "definition": view.definition,
            "comment": None,
            "provenance": "source",
        },
    )
    yield GraphEdge("HAS_VIEW", "Schema", {"id": schema_id}, "View", {"id": view_id})


def _emit_foreign_keys(table: RawTable, schema_id: str) -> Iterator[GraphEdge]:
    table_id = _table_id(schema_id, table.name)
    for fk in table.foreign_keys:
        ref_table_id = _table_id(schema_id, fk.ref_table)
        for constrained, referred in zip(fk.columns, fk.ref_columns):
            yield GraphEdge(
                "REFERENCES",
                "Column",
                {"id": _column_id(table_id, constrained)},
                "Column",
                {"id": _column_id(ref_table_id, referred)},
                {"constraint_name": fk.name, "on_delete": None, "on_update": None, "provenance": "source"},
            )


def _emit_view_reads(view: RawView, schema_id: str) -> Iterator[GraphEdge]:
    view_id = _view_id(schema_id, view.name)
    for table_name in view.reads:
        yield GraphEdge(
            "READS",
            "View",
            {"id": view_id},
            "Table",
            {"id": _table_id(schema_id, table_name)},
            {"provenance": "source"},
        )


# -- Merging / de-duplication (one node per name across the export's many files) -----


def _dedupe_tables(tables: Iterable[RawTable]) -> list[RawTable]:
    out: dict[str, RawTable] = {}
    for t in tables:
        out[t.name.upper()] = t  # last CREATE wins
    return list(out.values())


def _dedupe_by_name(items: Iterable) -> list:
    out: dict[str, object] = {}
    for item in items:
        out.setdefault(item.name.upper(), item)
    return list(out.values())


def _apply_alterations(tables: list[RawTable], alterations) -> None:
    """Fold `ALTER TABLE ... ADD ...` additions onto the by-name table (creating a
    partial one if only ALTERed here), so a table split across CREATE + ALTER scripts
    reconstructs fully. Mutates `tables`."""
    by_name = {t.name.upper(): t for t in tables}
    for alteration in alterations:
        table = by_name.get(alteration.table.upper())
        if table is None:
            table = RawTable(name=alteration.table)
            by_name[alteration.table.upper()] = table
            tables.append(table)
        for column in alteration.columns:
            column.position = len(table.columns) + 1
            table.columns.append(column)
        table.constraints.extend(alteration.constraints)
        table.foreign_keys.extend(alteration.foreign_keys)


def _referenced_tables(tables: list[RawTable], views: list[RawView], indexes: list[RawIndexDef]) -> set[str]:
    names: set[str] = set()
    for table in tables:
        for fk in table.foreign_keys:
            names.add(fk.ref_table)
    for view in views:
        names.update(view.reads)
    for index in indexes:
        names.add(index.table)
    return names
