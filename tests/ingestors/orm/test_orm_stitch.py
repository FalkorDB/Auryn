"""The ORM model<->table stitch (auryn-orm): a SQLAlchemy model class in a Python code graph is
joined to the physical Table/Column nodes a SQL ingestor produced, by table/column name -- the
code->database ("project A -> project B") connection. Needs FalkorDB (the conftest fixture)."""

from __future__ import annotations

from auryn.core import ingest
from auryn.core.graph_client import GraphWriter
from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.ingestors.orm.stitch import _parse_model, stitch

_MODEL_SRC = (
    "from sqlalchemy import Column, Integer, String\n"
    "from sqlalchemy.orm import declarative_base\n\n"
    "Base = declarative_base()\n\n\n"
    "class User(Base):\n"
    '    __tablename__ = "users"\n'
    "    id = Column(Integer, primary_key=True)\n"
    "    email = Column(String)\n"
    '    full_name = Column("name", String)\n'   # explicit column name differs from the attribute
    "\n\n"
    "def unrelated(x):\n"
    "    return x + 1\n"
)


def _seed_sql_schema(writer: GraphWriter) -> None:
    """Simulate a SQL ingestor's output: a `users` Table with id/email/name Columns."""
    writer.merge_node(GraphNode("Table", {"id": "sql://app/table/users"}, {"name": "users"}))
    for col in ("id", "email", "name"):
        cid = f"sql://app/table/users/column/{col}"
        writer.merge_node(GraphNode("Column", {"id": cid}, {"name": col}))
        writer.merge_edge(GraphEdge("HAS_COLUMN", "Table", {"id": "sql://app/table/users"}, "Column", {"id": cid}))


def _seed_scoped_table(writer: GraphWriter, schema_id: str, table_name: str, columns) -> str:
    """Seed a Table under a real (:Schema)-[:HAS_TABLE]->(:Table) scope, as a SQL ingestor emits it.
    The `schema_id` carries the dialect scheme (`oracle://...`, `postgresql://...`) the stitch reads to
    decide case-folding. Returns the table id."""
    writer.merge_node(GraphNode("Schema", {"id": schema_id}, {"name": schema_id}))
    tid = f"{schema_id}.{table_name}"
    writer.merge_node(GraphNode("Table", {"id": tid}, {"name": table_name}))
    writer.merge_edge(GraphEdge("HAS_TABLE", "Schema", {"id": schema_id}, "Table", {"id": tid}))
    for col in columns:
        cid = f"{tid}.{col}"
        writer.merge_node(GraphNode("Column", {"id": cid}, {"name": col}))
        writer.merge_edge(GraphEdge("HAS_COLUMN", "Table", {"id": tid}, "Column", {"id": cid}))
    return tid


def test_parse_model_recognizes_sqlalchemy_model():
    parsed = _parse_model("class User(Base):\n    __tablename__ = 'users'\n    id = Column(Integer)\n")
    assert parsed is not None
    tablename, columns = parsed
    assert tablename == "users"
    assert ("id", "id") in columns


def test_parse_model_ignores_plain_class():
    assert _parse_model("class Point:\n    def __init__(self):\n        self.x = 0\n") is None


def test_parse_model_explicit_column_name():
    _, cols = _parse_model('class U(Base):\n    __tablename__="u"\n    full_name = Column("name", String)\n')
    assert ("full_name", "name") in cols


def test_parse_model_name_keyword():
    """A `name="..."` keyword (no positional string) still names the physical column."""
    _, cols = _parse_model(
        'class U(Base):\n    __tablename__="u"\n    full_name = mapped_column(String, name="name")\n'
    )
    assert ("full_name", "name") in cols


def test_parse_model_annotation_only_mapped_column():
    """SQLAlchemy 2.0 infers a column for an annotation-only `x: Mapped[...]` (no assigned value),
    while an ordinary class annotation (`x: int`) is not a column."""
    _, cols = _parse_model(
        "class U(Base):\n"
        '    __tablename__ = "u"\n'
        "    id: Mapped[int] = mapped_column(primary_key=True)\n"
        "    email: Mapped[str]\n"          # annotation-only -> inferred column
        "    not_a_column: int\n"           # plain annotation -> NOT a column
    )
    names = {attr for attr, _ in cols}
    assert "id" in names and "email" in names
    assert "not_a_column" not in names


def test_stitch_maps_model_to_table_and_columns(graph, tmp_path):
    (tmp_path / "models.py").write_text(_MODEL_SRC)
    ingest.ingest_directory(GraphWriter(graph), str(tmp_path), project_name="app")
    writer = GraphWriter(graph)
    _seed_sql_schema(writer)

    report = stitch(writer)
    assert report is not None and report["models"] == 1

    def q(cypher):
        return [r[0] for r in graph.query(cypher).result_set]

    # The model class maps to the physical table.
    assert q("MATCH (:Class {name:'User'})-[:MAPS_TO]->(t:Table) RETURN t.name") == ["users"]
    # Each mapped column is reachable from the model (incl. the explicit-name column `name`).
    assert sorted(q("MATCH (:Class {name:'User'})-[:MAPS_TO]->(c:Column) RETURN c.name")) == ["email", "id", "name"]
    # The unrelated function/class is untouched.
    assert q("MATCH (:Function {name:'unrelated'})-[:MAPS_TO]->() RETURN 1") == []


def _seed_oracle_schema(writer: GraphWriter) -> None:
    """Simulate an Oracle ingestor: unquoted identifiers are folded to UPPER (USERS / ID, EMAIL, NAME)."""
    writer.merge_node(GraphNode("Table", {"id": "oracle://app/table/USERS"}, {"name": "USERS"}))
    for col in ("ID", "EMAIL", "NAME"):
        cid = f"oracle://app/table/USERS/column/{col}"
        writer.merge_node(GraphNode("Column", {"id": cid}, {"name": col}))
        writer.merge_edge(GraphEdge("HAS_COLUMN", "Table", {"id": "oracle://app/table/USERS"}, "Column", {"id": cid}))


def test_stitch_case_insensitive_table_match(graph, tmp_path):
    """A lowercase model (`__tablename__="users"`, `email`) must reach an Oracle UPPER schema
    (`USERS`, `EMAIL`) -- unquoted identifiers are case-insensitive across producers."""
    (tmp_path / "models.py").write_text(_MODEL_SRC)
    ingest.ingest_directory(GraphWriter(graph), str(tmp_path), project_name="app")
    writer = GraphWriter(graph)
    _seed_oracle_schema(writer)

    report = stitch(writer)
    assert report is not None and report["models"] == 1

    def q(cypher):
        return [r[0] for r in graph.query(cypher).result_set]

    assert q("MATCH (:Class {name:'User'})-[:MAPS_TO]->(t:Table) RETURN t.name") == ["USERS"]
    # lowercase model attrs (email, id) + the explicit lowercase `name` all reach the UPPER columns.
    assert sorted(q("MATCH (:Class {name:'User'})-[:MAPS_TO]->(c:Column) RETURN c.name")) == ["EMAIL", "ID", "NAME"]


def test_stitch_exact_case_wins_over_fold(graph, tmp_path):
    """When a schema holds two tables differing only by case (PostgreSQL `users` + quoted `"Users"`),
    a lowercase model links ONLY to the exact-case `users` -- the case-insensitive fallback fires only
    when no exact match exists (so Oracle UPPER still resolves, tested separately)."""
    (tmp_path / "models.py").write_text(_MODEL_SRC)
    ingest.ingest_directory(GraphWriter(graph), str(tmp_path), project_name="app")
    writer = GraphWriter(graph)
    _seed_sql_schema(writer)  # the exact-case `users` table
    # a second, case-distinct table in the same schema
    writer.merge_node(GraphNode("Table", {"id": "sql://app/table/Users"}, {"name": "Users"}))

    report = stitch(writer)
    assert report is not None and report["models"] == 1

    linked = [r[0] for r in graph.query(
        "MATCH (:Class {name:'User'})-[:MAPS_TO]->(t:Table) RETURN t.id"
    ).result_set]
    assert linked == ["sql://app/table/users"]  # only the exact-case table, not `Users`


def test_stitch_cross_producer_union(graph, tmp_path):
    """Per-scope matching unions across producers: a single lowercase model `users` maps to BOTH an
    Oracle `USERS` (UPPER-folded) and a PostgreSQL `users` (exact) when both schemas are ingested --
    the documented 1-to-many across dialects, which a global exact-first rule would have dropped."""
    (tmp_path / "models.py").write_text(_MODEL_SRC)
    ingest.ingest_directory(GraphWriter(graph), str(tmp_path), project_name="app")
    writer = GraphWriter(graph)
    _seed_scoped_table(writer, "oracle://app.APP", "USERS", ["ID", "EMAIL", "NAME"])
    _seed_scoped_table(writer, "postgresql://app.public", "users", ["id", "email", "name"])

    assert stitch(writer) is not None
    tnames = sorted(r[0] for r in graph.query(
        "MATCH (:Class {name:'User'})-[:MAPS_TO]->(t:Table) RETURN t.name"
    ).result_set)
    assert tnames == ["USERS", "users"]  # linked in BOTH producers


def test_stitch_postgres_quoted_no_false_match(graph, tmp_path):
    """A PostgreSQL schema whose only table is a quoted, case-preserved `"Users"` is a distinct relation
    from unquoted `users`; a lowercase model `__tablename__="users"` must NOT falsely link to it (the
    dialect fold is lower, so `users` folds to `users`, never reaching the stored `Users`)."""
    (tmp_path / "models.py").write_text(_MODEL_SRC)
    ingest.ingest_directory(GraphWriter(graph), str(tmp_path), project_name="app")
    writer = GraphWriter(graph)
    _seed_scoped_table(writer, "postgresql://app.public", "Users", ["Id"])

    assert stitch(writer) is None  # no model maps to any table
    assert graph.query(
        "MATCH (:Class {name:'User'})-[:MAPS_TO]->() RETURN count(*)"
    ).result_set[0][0] == 0


def test_stitch_is_idempotent(graph, tmp_path):
    (tmp_path / "models.py").write_text(_MODEL_SRC)
    ingest.ingest_directory(GraphWriter(graph), str(tmp_path), project_name="app")
    writer = GraphWriter(graph)
    _seed_sql_schema(writer)

    stitch(writer)
    first = graph.query("MATCH (:Class)-[r:MAPS_TO]->() RETURN count(r)").result_set[0][0]
    stitch(writer)  # a second run rebuilds its own layer -- no duplicates
    second = graph.query("MATCH (:Class)-[r:MAPS_TO]->() RETURN count(r)").result_set[0][0]
    assert first == second and first > 0
