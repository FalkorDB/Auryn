"""Integration test: build a small SQLite database, run SQLIngestor against a real
FalkorDB graph, and verify the resulting nodes/edges -- including idempotency."""

from __future__ import annotations

import sqlalchemy as sa

from auryn.core.graph_client import GraphWriter
from auryn.ingestors.sql.config import SQLSourceConfig
from auryn.ingestors.sql.ingestor import SQLIngestor


def _build_sample_db(db_path) -> str:
    dsn = f"sqlite:///{db_path}"
    engine = sa.create_engine(dsn)
    metadata = sa.MetaData()

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
        sa.CheckConstraint("title != ''", name="ck_books_title_not_empty"),
    )
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE VIEW author_names AS SELECT id, name FROM authors"))
    engine.dispose()
    return dsn


def _run_ingestor(dsn: str, graph):
    writer = GraphWriter(graph)
    ingestor = SQLIngestor(SQLSourceConfig(dsn=dsn, database_name="sample"))
    return ingestor.run(writer)


def test_sql_ingestor_creates_expected_graph(tmp_path, graph):
    dsn = _build_sample_db(tmp_path / "sample.db")

    stats = _run_ingestor(dsn, graph)

    # 1 Database + 1 Schema + 2 Tables + 6 Columns (authors: id,name,email; books: id,title,author_id)
    # + 1 Index (ix_authors_name) + 4 Constraints (authors PK+UNIQUE, books PK+CHECK)
    # + 1 View (author_names) + 2 view Columns (id, name)
    assert stats.nodes_merged == 18
    # HAS_SCHEMA(1) + HAS_TABLE(2) + HAS_COLUMN(6+2) + REFERENCES(1) + HAS_INDEX(1) + INDEXES(1)
    # + HAS_CONSTRAINT(4) + CONSTRAINS(3) + HAS_VIEW(1)
    assert stats.edges_merged == 22

    rows = graph.query(
        "MATCH (n) RETURN labels(n)[0] AS label, count(n) AS count"
    ).result_set
    counts_by_label = {row[0]: row[1] for row in rows}
    assert counts_by_label == {
        "Column": 8,
        "Database": 1,
        "Schema": 1,
        "Table": 2,
        "Index": 1,
        "Constraint": 4,
        "View": 1,
    }

    fk_rows = graph.query(
        """
        MATCH (child:Column {id: 'sqlite://sample.main.books.author_id'})
              -[r:REFERENCES]->
              (parent:Column {id: 'sqlite://sample.main.authors.id'})
        RETURN r.constraint_name, r.on_delete
        """
    ).result_set
    assert fk_rows == [["fk_books_author_id", "CASCADE"]]

    index_rows = graph.query(
        """
        MATCH (i:Index {id: 'sqlite://sample.main.authors.index.ix_authors_name'})
              -[:INDEXES]->(c:Column)
        RETURN i.unique, c.name
        """
    ).result_set
    assert index_rows == [[False, "name"]]

    unique_rows = graph.query(
        """
        MATCH (c:Constraint {id: 'sqlite://sample.main.authors.constraint.uq_authors_email'})
              -[:CONSTRAINS]->(col:Column)
        RETURN c.type, col.name
        """
    ).result_set
    assert unique_rows == [["UNIQUE", "email"]]

    check_rows = graph.query(
        "MATCH (c:Constraint {id: 'sqlite://sample.main.books.constraint.ck_books_title_not_empty'}) "
        "RETURN c.type, c.expression"
    ).result_set
    assert check_rows == [["CHECK", "title != ''"]]

    view_rows = graph.query(
        "MATCH (v:View {id: 'sqlite://sample.main.author_names'}) RETURN v.kind, v.definition"
    ).result_set
    assert view_rows == [["VIEW", "CREATE VIEW author_names AS SELECT id, name FROM authors"]]

    view_column_rows = graph.query(
        """
        MATCH (:View {id: 'sqlite://sample.main.author_names'})-[:HAS_COLUMN]->(c:Column)
        RETURN c.name ORDER BY c.name
        """
    ).result_set
    assert [row[0] for row in view_column_rows] == ["id", "name"]


def test_sql_ingestor_is_idempotent(tmp_path, graph):
    dsn = _build_sample_db(tmp_path / "sample.db")

    _run_ingestor(dsn, graph)
    counts_after_first_run = (
        graph.query("MATCH (n) RETURN count(n)").result_set[0][0],
        graph.query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0],
    )

    _run_ingestor(dsn, graph)
    counts_after_second_run = (
        graph.query("MATCH (n) RETURN count(n)").result_set[0][0],
        graph.query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0],
    )

    assert counts_after_first_run == counts_after_second_run


def test_sql_ingestor_sets_up_indexes_and_constraints(tmp_path, graph):
    dsn = _build_sample_db(tmp_path / "sample.db")

    _run_ingestor(dsn, graph)

    constraints = {(c["label"], tuple(c["properties"])) for c in graph.list_constraints()}
    assert ("Table", ("id",)) in constraints
    assert ("Column", ("id",)) in constraints
    assert ("Index", ("id",)) in constraints
    assert ("Constraint", ("id",)) in constraints
    assert ("View", ("id",)) in constraints


def test_sql_ingestor_models_composite_primary_key_as_one_constraint(tmp_path, graph):
    dsn = f"sqlite:///{tmp_path / 'composite.db'}"
    engine = sa.create_engine(dsn)
    metadata = sa.MetaData()
    sa.Table(
        "order_items",
        metadata,
        sa.Column("order_id", sa.Integer, primary_key=True),
        sa.Column("product_id", sa.Integer, primary_key=True),
        sa.Column("quantity", sa.Integer, nullable=False),
    )
    metadata.create_all(engine)
    engine.dispose()

    SQLIngestor(SQLSourceConfig(dsn=dsn, database_name="composite")).run(GraphWriter(graph))

    constraint_rows = graph.query("MATCH (c:Constraint {type: 'PRIMARY_KEY'}) RETURN c.id").result_set
    assert len(constraint_rows) == 1  # one Constraint node for the composite key, not two

    constrained_columns = graph.query(
        """
        MATCH (:Constraint {type: 'PRIMARY_KEY'})-[r:CONSTRAINS]->(col:Column)
        RETURN col.name ORDER BY r.position
        """
    ).result_set
    assert [row[0] for row in constrained_columns] == ["order_id", "product_id"]
