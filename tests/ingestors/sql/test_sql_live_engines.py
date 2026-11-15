"""Live-engine integration tests for the generic (SQLAlchemy-backed) SQL ingestor against
real PostgreSQL and MySQL testcontainers -- proving the one dialect-agnostic Inspector path
reflects every relational engine, not just SQLite. Each test is skipped when Docker is absent
(see conftest.py). Assertions are written to be dialect-robust: they key off structural counts,
the derived `dialect://` id prefix, and the FK edge's properties rather than hard-coded ids or
features one engine lacks (e.g. MySQL has no standalone sequences; CHECK semantics vary)."""

from __future__ import annotations

import sqlalchemy as sa

from auryn.core.graph_client import GraphWriter
from auryn.ingestors.sql.config import SQLSourceConfig
from auryn.ingestors.sql.ingestor import SQLIngestor


def _build_sample_schema(dsn: str, schema: str | None = None) -> None:
    """Create the same authors/books shape used by the SQLite test, against any engine."""
    engine = sa.create_engine(dsn)
    metadata = sa.MetaData(schema=schema)
    sa.Table(
        "authors",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, index=True),
        sa.Column("email", sa.String(200), nullable=False),
        sa.UniqueConstraint("email", name="uq_authors_email"),
    )
    sa.Table(
        "books",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column(
            "author_id",
            sa.Integer,
            sa.ForeignKey("authors.id", name="fk_books_author_id", ondelete="CASCADE"),
            nullable=False,
        ),
    )
    metadata.create_all(engine)
    engine.dispose()


def _ingest(dsn: str, graph, database_name: str, schemas=None):
    return SQLIngestor(
        SQLSourceConfig(dsn=dsn, database_name=database_name, schemas=schemas)
    ).run(GraphWriter(graph))


def _assert_authors_books_graph(graph, dialect_prefix: str):
    # The Database node records the SQLAlchemy dialect it reflected.
    db = graph.query("MATCH (d:Database) RETURN d.dialect, d.name").result_set
    assert len(db) == 1
    assert db[0][0] == dialect_prefix

    labels = {
        row[0]: row[1]
        for row in graph.query(
            "MATCH (n) RETURN labels(n)[0] AS label, count(n) AS c"
        ).result_set
    }
    assert labels.get("Database") == 1
    assert labels.get("Table") == 2
    # authors(id,name,email) + books(id,title,author_id) = 6 table columns.
    assert labels.get("Column") == 6

    # The FK reflects as a column->column REFERENCES edge carrying its constraint metadata,
    # regardless of dialect. (MySQL/Postgres both report ondelete.)
    fk = graph.query(
        """
        MATCH (child:Column {name: 'author_id'})-[r:REFERENCES]->(parent:Column {name: 'id'})
        RETURN r.constraint_name, r.on_delete
        """
    ).result_set
    assert fk == [["fk_books_author_id", "CASCADE"]]

    # The non-unique index on authors.name reflects everywhere.
    idx = graph.query(
        "MATCH (i:Index)-[:INDEXES]->(c:Column {name: 'name'}) RETURN count(i)"
    ).result_set
    assert idx[0][0] >= 1

    # A PRIMARY_KEY constraint on each table.
    pks = graph.query(
        "MATCH (c:Constraint {type: 'PRIMARY_KEY'}) RETURN count(c)"
    ).result_set
    assert pks[0][0] == 2


def test_postgres_schema_reflection(postgres_dsn, graph):
    _build_sample_schema(postgres_dsn)  # lands in the default "public" schema
    stats = _ingest(postgres_dsn, graph, database_name="pgsample")
    assert stats.nodes_merged > 0
    _assert_authors_books_graph(graph, dialect_prefix="postgresql")


def test_postgres_reingest_is_idempotent(postgres_dsn, graph):
    _build_sample_schema(postgres_dsn)
    _ingest(postgres_dsn, graph, database_name="pgsample")
    before = (
        graph.query("MATCH (n) RETURN count(n)").result_set[0][0],
        graph.query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0],
    )
    _ingest(postgres_dsn, graph, database_name="pgsample")
    after = (
        graph.query("MATCH (n) RETURN count(n)").result_set[0][0],
        graph.query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0],
    )
    assert before == after


def test_mysql_schema_reflection(mysql_dsn, graph):
    # On MySQL a "schema" IS a database; the container's DB is named "test". Restrict to it
    # so get_schema_names() (which lists every DB on the server) doesn't drag in sys schemas.
    _build_sample_schema(mysql_dsn)
    stats = _ingest(mysql_dsn, graph, database_name="mysample", schemas=("test",))
    assert stats.nodes_merged > 0
    _assert_authors_books_graph(graph, dialect_prefix="mysql")
