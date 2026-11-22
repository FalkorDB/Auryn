"""Integration test: seed a graph via SQLIngestor, then drive the viewer's Flask API
against a real FalkorDB -- reusing the same fixtures as the SQL ingestor tests."""

from __future__ import annotations

import sqlalchemy as sa

from auryn.core.graph_client import GraphWriter
from auryn.ingestors.sql.config import SQLSourceConfig
from auryn.ingestors.sql.ingestor import SQLIngestor
from auryn.viewer.app import create_app


def _build_sample_db(db_path) -> str:
    dsn = f"sqlite:///{db_path}"
    engine = sa.create_engine(dsn)
    metadata = sa.MetaData()

    sa.Table(
        "authors",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
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
    return dsn


def _seeded_client(tmp_path, graph, falkordb_connection):
    """Ingests the sample DB into `graph`, returns (Flask test client, graph name)."""
    dsn = _build_sample_db(tmp_path / "sample.db")
    SQLIngestor(SQLSourceConfig(dsn=dsn, database_name="sample")).run(GraphWriter(graph))
    app = create_app(falkordb_connection)
    return app.test_client(), graph.name


def test_list_graphs_includes_seeded_graph(tmp_path, graph, falkordb_connection):
    client, graph_name = _seeded_client(tmp_path, graph, falkordb_connection)

    resp = client.get("/api/graphs")

    assert resp.status_code == 200
    assert graph_name in resp.get_json()


def test_loopback_viewer_serves_loopback_host_names_only(falkordb_connection):
    """DNS rebinding: a page that resolves its own name to 127.0.0.1 reaches the viewer with THAT
    name in Host. Bound to loopback (the default) it answers only to loopback names; bound
    elsewhere on purpose (`--host 0.0.0.0`) it serves whatever name it is reached under."""
    client = create_app(falkordb_connection).test_client()
    for host in ("localhost", "LOCALHOST:8080", "localhost.:8080", "127.0.0.1:8080", "127.0.0.2:8080", "[::1]:8080", "[::1]"):
        assert client.get("/api/graphs", headers={"Host": host}).status_code == 200, host
    for host in ("evil.example", "evil.example:8080", "localhost.evil.example:8080", "10.0.0.5:8080"):
        assert client.get("/api/graphs", headers={"Host": host}).status_code == 403, host

    # a loopback bind is one whatever its spelling -- the guard must not depend on the literal
    for bind in ("localhost", "127.0.0.2", "::1", "0:0:0:0:0:0:0:1"):
        other = create_app(falkordb_connection, bind).test_client()
        assert other.get("/api/graphs", headers={"Host": "evil.example:8080"}).status_code == 403, bind
        assert other.get("/api/graphs", headers={"Host": "127.0.0.1:8080"}).status_code == 200, bind

    # ...including spellings only the OS resolver accepts (all bind 127.0.0.1); an unresolvable
    # bind name fails closed
    for bind in ("127.1", "2130706433", "0x7f.1", "no-such-host.invalid"):
        other = create_app(falkordb_connection, bind).test_client()
        assert other.get("/api/graphs", headers={"Host": "evil.example:8080"}).status_code == 403, bind
        assert other.get("/api/graphs", headers={"Host": f"{bind}:8080"}).status_code == 200, bind

    exposed = create_app(falkordb_connection, "0.0.0.0").test_client()
    assert exposed.get("/api/graphs", headers={"Host": "box.lan:8080"}).status_code == 200


def test_overview_reports_correct_counts(tmp_path, graph, falkordb_connection):
    client, graph_name = _seeded_client(tmp_path, graph, falkordb_connection)

    resp = client.get(f"/api/graphs/{graph_name}/overview")

    assert resp.status_code == 200
    data = resp.get_json()
    node_counts = {row["label"]: row["count"] for row in data["nodes"]}
    edge_counts = {row["type"]: row["count"] for row in data["edges"]}
    # authors/books each have an (anonymous, single-column) primary key, which the SQL
    # ingestor now models as its own Constraint node rather than just a Column flag.
    assert node_counts == {"Database": 1, "Schema": 1, "Table": 2, "Column": 5, "Constraint": 2}
    assert edge_counts == {
        "HAS_SCHEMA": 1,
        "HAS_TABLE": 2,
        "HAS_COLUMN": 5,
        "REFERENCES": 1,
        "HAS_CONSTRAINT": 2,
        "CONSTRAINS": 2,
    }


def test_all_returns_every_node_and_edge_untruncated(tmp_path, graph, falkordb_connection):
    client, graph_name = _seeded_client(tmp_path, graph, falkordb_connection)

    resp = client.get(f"/api/graphs/{graph_name}/all")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["truncated"] is False
    # Database, Schema, 2 Table, 5 Column, 2 Constraint (see test_overview above)
    assert len(data["nodes"]) == 11
    assert len(data["edges"]) == sum(
        [1, 2, 5, 1, 2, 2]
    )  # HAS_SCHEMA, HAS_TABLE, HAS_COLUMN, REFERENCES, HAS_CONSTRAINT, CONSTRAINS


def test_all_respects_a_limit_and_reports_truncation(tmp_path, graph, falkordb_connection):
    client, graph_name = _seeded_client(tmp_path, graph, falkordb_connection)

    resp = client.get(f"/api/graphs/{graph_name}/all", query_string={"limit": 3})

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["truncated"] is True
    assert len(data["nodes"]) == 3
    included_ids = {n["id"] for n in data["nodes"]}
    # Every edge's endpoints must be among the 3 nodes actually returned -- an edge
    # to a node outside the cap would make vis-network error out client-side.
    for edge in data["edges"]:
        assert edge["from"] in included_ids
        assert edge["to"] in included_ids


def test_roots_returns_only_the_database_node(tmp_path, graph, falkordb_connection):
    client, graph_name = _seeded_client(tmp_path, graph, falkordb_connection)

    resp = client.get(f"/api/graphs/{graph_name}/roots")

    assert resp.status_code == 200
    roots = resp.get_json()
    assert len(roots) == 1
    assert roots[0]["label"] == "Database"
    assert roots[0]["id"] == "sqlite://sample"


def test_search_finds_table_by_name(tmp_path, graph, falkordb_connection):
    client, graph_name = _seeded_client(tmp_path, graph, falkordb_connection)

    resp = client.get(f"/api/graphs/{graph_name}/search", query_string={"label": "Table", "q": "book"})

    assert resp.status_code == 200
    assert [row["name"] for row in resp.get_json()] == ["books"]


def test_node_detail_returns_full_properties(tmp_path, graph, falkordb_connection):
    client, graph_name = _seeded_client(tmp_path, graph, falkordb_connection)

    resp = client.get(f"/api/graphs/{graph_name}/node", query_string={"id": "sqlite://sample"})

    assert resp.status_code == 200
    node = resp.get_json()
    assert node["label"] == "Database"
    assert node["properties"]["name"] == "sample"


def test_node_detail_missing_node_returns_404(tmp_path, graph, falkordb_connection):
    client, graph_name = _seeded_client(tmp_path, graph, falkordb_connection)

    resp = client.get(f"/api/graphs/{graph_name}/node", query_string={"id": "does-not-exist"})

    assert resp.status_code == 404


def test_neighbors_expands_database_to_schema(tmp_path, graph, falkordb_connection):
    client, graph_name = _seeded_client(tmp_path, graph, falkordb_connection)

    resp = client.get(f"/api/graphs/{graph_name}/neighbors", query_string={"id": "sqlite://sample"})

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["center"]["label"] == "Database"
    assert [n["label"] for n in data["nodes"]] == ["Schema"]
    assert data["edges"] == [
        {
            "from": "sqlite://sample",
            "to": "sqlite://sample.main",
            "type": "HAS_SCHEMA",
            "properties": {},
        }
    ]


def test_path_finds_directed_connection(tmp_path, graph, falkordb_connection):
    client, graph_name = _seeded_client(tmp_path, graph, falkordb_connection)

    resp = client.get(
        f"/api/graphs/{graph_name}/path",
        query_string={"from": "sqlite://sample", "to": "sqlite://sample.main"},
    )

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["found"] is True
    assert data["hops"] == 1
    assert [n["id"] for n in data["nodes"]] == ["sqlite://sample", "sqlite://sample.main"]
    assert (data["edges"][0]["from"], data["edges"][0]["type"], data["edges"][0]["to"]) == (
        "sqlite://sample", "HAS_SCHEMA", "sqlite://sample.main",
    )


def test_path_direction_matters(tmp_path, graph, falkordb_connection):
    client, graph_name = _seeded_client(tmp_path, graph, falkordb_connection)

    # the schema doesn't point back up to the database, so directed finds nothing...
    directed = client.get(
        f"/api/graphs/{graph_name}/path",
        query_string={"from": "sqlite://sample.main", "to": "sqlite://sample", "directed": "true"},
    ).get_json()
    assert directed["found"] is False

    # ...but undirected traverses the HAS_SCHEMA edge backwards and connects them.
    undirected = client.get(
        f"/api/graphs/{graph_name}/path",
        query_string={"from": "sqlite://sample.main", "to": "sqlite://sample", "directed": "false"},
    ).get_json()
    assert undirected["found"] is True
    assert undirected["hops"] == 1


def test_path_requires_both_endpoints(tmp_path, graph, falkordb_connection):
    client, graph_name = _seeded_client(tmp_path, graph, falkordb_connection)

    resp = client.get(f"/api/graphs/{graph_name}/path", query_string={"from": "sqlite://sample"})

    assert resp.status_code == 400


def test_path_missing_node_is_404(tmp_path, graph, falkordb_connection):
    client, graph_name = _seeded_client(tmp_path, graph, falkordb_connection)

    resp = client.get(
        f"/api/graphs/{graph_name}/path",
        query_string={"from": "sqlite://sample", "to": "does-not-exist"},
    )

    assert resp.status_code == 404
