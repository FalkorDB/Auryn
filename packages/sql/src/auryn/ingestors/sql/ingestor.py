"""Extracts Database/Schema/Table/Column structure (plus foreign keys, indexes,
constraints, views, and sequences) from a SQL database, via SQLAlchemy's
dialect-agnostic Inspector."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

import click

from auryn.core.base_ingestor import BaseIngestor
from auryn.core.graph_client import GraphWriter, connect
from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.core.plugins import ensure_importable
from auryn.core.registry import register_ingestor
from auryn.ingestors.sql.config import SQLSourceConfig
from auryn.ingestors.sql.schema import EDGE_SCHEMAS, NODE_SCHEMAS

if TYPE_CHECKING:  # for type-checkers only -- never imported at runtime
    import sqlalchemy as sa
    from sqlalchemy.engine.reflection import Inspector

# SQLAlchemy + the per-dialect DBAPI driver are an optional extra (`auryn-oracle[live]`):
# the file-based `plsql` ingestor never touches them, so a lean install omits them. They're
# imported on demand in extract() (auto-installed on first live run), and the specific DBAPI
# module SQLAlchemy needs for the DSN's dialect is auto-installed too -- these are the specs
# used for that.
_DBAPI_SPECS = {
    "oracledb": "oracledb>=4.0.2",
    "cx_Oracle": "oracledb>=4.0.2",
    "psycopg2": "psycopg2-binary>=2.9",
    "pymysql": "pymysql>=1.1",
}


@register_ingestor("sql")
class SQLIngestor(BaseIngestor):
    node_schemas = NODE_SCHEMAS
    edge_schemas = EDGE_SCHEMAS

    def __init__(self, config: SQLSourceConfig) -> None:
        self._config = config

    @classmethod
    def build_cli_command(cls) -> click.Command:
        @click.command(
            "sql",
            help=(
                "Ingest a SQL database's schema (tables, columns, foreign keys, "
                "indexes, constraints, views, sequences)."
            ),
        )
        @click.option("--dsn", required=True, help="SQLAlchemy connection URL for the source database.")
        @click.option(
            "--database-name", required=True, help="Logical name for this source (used to build node ids)."
        )
        @click.option(
            "--schema",
            "schemas",
            multiple=True,
            help="Restrict ingestion to this schema (repeatable). Default: connection's default schema only.",
        )
        @click.pass_context
        def command(ctx: click.Context, dsn: str, database_name: str, schemas: tuple[str, ...]) -> None:
            writer = GraphWriter(connect(ctx.obj["falkordb_config"]))
            stats = cls(
                SQLSourceConfig(dsn=dsn, database_name=database_name, schemas=schemas or None)
            ).run(writer)
            click.echo(
                f"Merged {stats.nodes_merged} nodes, {stats.edges_merged} edges "
                f"in {stats.elapsed_seconds:.2f}s"
            )

        return command

    def extract(self) -> Iterator[GraphNode | GraphEdge]:
        sa = ensure_importable("sqlalchemy", "sqlalchemy>=2.0")  # optional [live] extra, on demand
        engine = self._engine(sa)
        try:
            inspector = sa.inspect(engine)
            dialect = engine.dialect.name
            database_id = f"{dialect}://{self._config.database_name}"

            yield GraphNode(
                label="Database",
                key={"id": database_id},
                properties={"name": self._config.database_name, "dialect": dialect},
            )

            schema_names = self._resolve_schemas(inspector)

            # Pass 1: every node (and non-FK edge), so all Columns exist before pass 2.
            for schema_name in schema_names:
                yield from self._extract_schema(inspector, database_id, schema_name)

            # Pass 2: FK edges, once every table's columns are guaranteed to exist.
            # Doing this inline during pass 1 would silently drop forward references
            # (e.g. a table processed before the one it references, which get_table_names()
            # gives no ordering guarantee against) -- MERGE on a MATCH that finds nothing
            # is a no-op, not an error, so this would fail silently rather than loudly.
            for schema_name in schema_names:
                yield from self._extract_foreign_keys_for_schema(inspector, database_id, schema_name)
        finally:
            engine.dispose()

    def _engine(self, sa):
        """Build an engine whose per-dialect DBAPI driver is guaranteed importable, probing
        the connection once so a missing driver (e.g. oracledb for an `oracle+oracledb://`
        DSN) is auto-installed here rather than surfacing mid-reflection. Real connection
        errors (bad DSN, DB down) propagate unchanged."""
        for attempt in (1, 2):
            engine = sa.create_engine(self._config.dsn)
            try:
                engine.connect().close()  # forces the DBAPI import for this dialect
                return engine
            except ModuleNotFoundError as missing:
                engine.dispose()
                if attempt == 2:
                    raise
                ensure_importable(missing.name, _DBAPI_SPECS.get(missing.name or "", missing.name or ""))
        raise RuntimeError("unreachable")  # pragma: no cover

    def _extract_foreign_keys_for_schema(
        self, inspector: Inspector, database_id: str, schema_name: str
    ) -> Iterator[GraphEdge]:
        schema_id = f"{database_id}.{schema_name}"
        for table_name in inspector.get_table_names(schema=schema_name):
            table_id = f"{schema_id}.{table_name}"
            for fk in inspector.get_foreign_keys(table_name, schema=schema_name):
                yield from self._extract_foreign_keys(database_id, schema_name, table_id, fk)

    def _resolve_schemas(self, inspector: Inspector) -> list[str]:
        if self._config.schemas is not None:
            return list(self._config.schemas)
        # Restrict to the connection's default schema unless told otherwise -- this
        # matters most on MySQL, where get_schema_names() lists every database on
        # the server, not just the one named in the DSN.
        default_schema = inspector.default_schema_name
        if default_schema:
            return [default_schema]
        return inspector.get_schema_names() or ["main"]

    def _extract_schema(
        self, inspector: Inspector, database_id: str, schema_name: str
    ) -> Iterator[GraphNode | GraphEdge]:
        schema_id = f"{database_id}.{schema_name}"
        yield GraphNode(label="Schema", key={"id": schema_id}, properties={"name": schema_name})
        yield GraphEdge(
            rel_type="HAS_SCHEMA",
            from_label="Database",
            from_key={"id": database_id},
            to_label="Schema",
            to_key={"id": schema_id},
        )

        for table_name in inspector.get_table_names(schema=schema_name):
            yield from self._extract_table(inspector, schema_id, schema_name, table_name)

        for view_name in inspector.get_view_names(schema=schema_name):
            yield from self._extract_view(inspector, schema_id, schema_name, view_name, "VIEW")
        for view_name in self._materialized_view_names(inspector, schema_name):
            yield from self._extract_view(
                inspector, schema_id, schema_name, view_name, "MATERIALIZED_VIEW"
            )

        for sequence_name in self._sequence_names(inspector, schema_name):
            yield from self._extract_sequence(schema_id, sequence_name)

    def _extract_table(
        self,
        inspector: Inspector,
        schema_id: str,
        schema_name: str,
        table_name: str,
    ) -> Iterator[GraphNode | GraphEdge]:
        table_id = f"{schema_id}.{table_name}"
        yield GraphNode(
            label="Table",
            key={"id": table_id},
            properties={
                "name": table_name,
                "comment": self._table_comment(inspector, schema_name, table_name),
            },
        )
        yield GraphEdge(
            rel_type="HAS_TABLE",
            from_label="Schema",
            from_key={"id": schema_id},
            to_label="Table",
            to_key={"id": table_id},
        )

        pk = inspector.get_pk_constraint(table_name, schema=schema_name)
        pk_columns = set(pk.get("constrained_columns") or [])

        for position, column in enumerate(
            inspector.get_columns(table_name, schema=schema_name), start=1
        ):
            yield from self._extract_column("Table", table_id, position, column, pk_columns)

        # FK edges are deferred to a second pass over the whole database (see extract()) --
        # not extracted here, even though this method has everything needed to do it.

        for index in inspector.get_indexes(table_name, schema=schema_name):
            yield from self._extract_index(table_id, index)

        if pk_columns:
            yield from self._extract_constraint(
                table_id, "PRIMARY_KEY", pk.get("name"), 0, column_names=pk["constrained_columns"]
            )
        for position, unique in enumerate(
            inspector.get_unique_constraints(table_name, schema=schema_name)
        ):
            yield from self._extract_constraint(
                table_id, "UNIQUE", unique.get("name"), position, column_names=unique["column_names"]
            )
        for position, check in enumerate(
            inspector.get_check_constraints(table_name, schema=schema_name)
        ):
            yield from self._extract_constraint(
                table_id, "CHECK", check.get("name"), position, expression=check.get("sqltext")
            )

    @staticmethod
    def _table_comment(inspector: Inspector, schema_name: str, table_name: str) -> str | None:
        try:
            return inspector.get_table_comment(table_name, schema=schema_name).get("text")
        except NotImplementedError:
            return None

    @staticmethod
    def _materialized_view_names(inspector: Inspector, schema_name: str) -> list[str]:
        try:
            return inspector.get_materialized_view_names(schema=schema_name)
        except NotImplementedError:
            return []  # dialect doesn't support materialized views (e.g. SQLite, MySQL)

    @staticmethod
    def _sequence_names(inspector: Inspector, schema_name: str) -> list[str]:
        try:
            return inspector.get_sequence_names(schema=schema_name)
        except NotImplementedError:
            return []  # dialect has no standalone sequences (e.g. SQLite, MySQL)

    def _extract_view(
        self,
        inspector: Inspector,
        schema_id: str,
        schema_name: str,
        view_name: str,
        kind: str,
    ) -> Iterator[GraphNode | GraphEdge]:
        view_id = f"{schema_id}.{view_name}"
        yield GraphNode(
            label="View",
            key={"id": view_id},
            properties={
                "name": view_name,
                "kind": kind,
                "definition": self._view_definition(inspector, schema_name, view_name),
                "comment": self._table_comment(inspector, schema_name, view_name),
            },
        )
        yield GraphEdge(
            rel_type="HAS_VIEW",
            from_label="Schema",
            from_key={"id": schema_id},
            to_label="View",
            to_key={"id": view_id},
        )
        # Views don't have their own primary key; columns just aren't part of one.
        # Note: this only captures the view's own column list, not what tables/views
        # it reads from -- that would need either dialect-specific catalog queries or
        # parsing `definition`, neither of which fits the generic Inspector approach
        # used everywhere else here. Left as a known gap.
        for position, column in enumerate(
            inspector.get_columns(view_name, schema=schema_name), start=1
        ):
            yield from self._extract_column("View", view_id, position, column, set())

    @staticmethod
    def _view_definition(inspector: Inspector, schema_name: str, view_name: str) -> str | None:
        import sqlalchemy as sa  # guaranteed importable by the time extract() reaches here

        try:
            return inspector.get_view_definition(view_name, schema=schema_name)
        except (NotImplementedError, sa.exc.NoSuchTableError):
            # NoSuchTableError: observed against Oracle for certain built-in/internal
            # views get_view_names() lists but the dialect can't actually introspect
            # further (e.g. SYSTEM.MVIEW_WORKLOAD on Oracle XE) -- not just a
            # hypothetical, hit this against a real instance.
            return None

    @staticmethod
    def _extract_sequence(schema_id: str, sequence_name: str) -> Iterator[GraphNode | GraphEdge]:
        sequence_id = f"{schema_id}.sequence.{sequence_name}"
        yield GraphNode(
            label="Sequence",
            key={"id": sequence_id},
            properties={"name": sequence_name},
        )
        yield GraphEdge(
            rel_type="HAS_SEQUENCE",
            from_label="Schema",
            from_key={"id": schema_id},
            to_label="Sequence",
            to_key={"id": sequence_id},
        )

    @staticmethod
    def _extract_column(
        parent_label: str, parent_id: str, position: int, column: dict, pk_columns: set[str]
    ) -> Iterator[GraphNode | GraphEdge]:
        column_name = column["name"]
        column_id = f"{parent_id}.{column_name}"
        yield GraphNode(
            label="Column",
            key={"id": column_id},
            properties={
                "name": column_name,
                "ordinal_position": position,
                "data_type": str(column["type"]),
                "nullable": bool(column.get("nullable", True)),
                "default_value": column.get("default"),
                "is_primary_key": column_name in pk_columns,
                "is_autoincrement": bool(column.get("autoincrement", False)),
                "comment": column.get("comment"),
            },
        )
        yield GraphEdge(
            rel_type="HAS_COLUMN",
            from_label=parent_label,
            from_key={"id": parent_id},
            to_label="Column",
            to_key={"id": column_id},
        )

    @staticmethod
    def _extract_index(table_id: str, index: dict) -> Iterator[GraphNode | GraphEdge]:
        index_name = index.get("name") or "unnamed_index"
        index_id = f"{table_id}.index.{index_name}"
        yield GraphNode(
            label="Index",
            key={"id": index_id},
            properties={"name": index_name, "unique": bool(index.get("unique", False))},
        )
        yield GraphEdge(
            rel_type="HAS_INDEX",
            from_label="Table",
            from_key={"id": table_id},
            to_label="Index",
            to_key={"id": index_id},
        )
        for position, column_name in enumerate(index.get("column_names") or [], start=1):
            if column_name is None:
                continue  # expression-based index members reflect as None, not a column
            yield GraphEdge(
                rel_type="INDEXES",
                from_label="Index",
                from_key={"id": index_id},
                to_label="Column",
                to_key={"id": f"{table_id}.{column_name}"},
                properties={"position": position},
            )

    @staticmethod
    def _extract_constraint(
        table_id: str,
        constraint_type: str,
        name: str | None,
        ordinal: int,
        column_names: tuple[str, ...] = (),
        expression: str | None = None,
    ) -> Iterator[GraphNode | GraphEdge]:
        # Falls back to a positional name (e.g. "unique_0") when the dialect doesn't
        # preserve one (PK names are commonly lost on reflection, e.g. SQLite).
        # Stable only as long as the underlying constraint set doesn't change between
        # runs -- unlike named constraints, these can be "renamed" by reordering.
        constraint_name = name or f"{constraint_type.lower()}_{ordinal}"
        constraint_id = f"{table_id}.constraint.{constraint_name}"
        yield GraphNode(
            label="Constraint",
            key={"id": constraint_id},
            properties={"name": constraint_name, "type": constraint_type, "expression": expression},
        )
        yield GraphEdge(
            rel_type="HAS_CONSTRAINT",
            from_label="Table",
            from_key={"id": table_id},
            to_label="Constraint",
            to_key={"id": constraint_id},
        )
        for position, column_name in enumerate(column_names, start=1):
            yield GraphEdge(
                rel_type="CONSTRAINS",
                from_label="Constraint",
                from_key={"id": constraint_id},
                to_label="Column",
                to_key={"id": f"{table_id}.{column_name}"},
                properties={"position": position},
            )

    @staticmethod
    def _extract_foreign_keys(
        database_id: str, schema_name: str, table_id: str, fk: dict
    ) -> Iterator[GraphEdge]:
        referred_schema = fk.get("referred_schema") or schema_name
        referred_table_id = f"{database_id}.{referred_schema}.{fk['referred_table']}"
        options = fk.get("options") or {}
        properties = {
            "constraint_name": fk.get("name"),
            "on_delete": options.get("ondelete"),
            "on_update": options.get("onupdate"),
        }
        # Composite FKs constrain multiple columns at once; emit one column-to-column
        # edge per pair rather than dropping all but the first.
        for constrained_column, referred_column in zip(
            fk["constrained_columns"], fk["referred_columns"]
        ):
            yield GraphEdge(
                rel_type="REFERENCES",
                from_label="Column",
                from_key={"id": f"{table_id}.{constrained_column}"},
                to_label="Column",
                to_key={"id": f"{referred_table_id}.{referred_column}"},
                properties=properties,
            )
