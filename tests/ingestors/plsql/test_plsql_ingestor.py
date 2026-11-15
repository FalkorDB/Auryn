"""Integration tests against a real Oracle XE testcontainer (see oracle_schema in
tests/conftest.py for the sample schema: an employees table, log_greeting/greet
procedures where greet calls log_greeting, a greet_count function, a greetings_pkg
package (greet_all/double_it/an overloaded notify pair), and a trigger on employees.

Seeds via SQLIngestor first so the Database/Schema/Table nodes PLSQLIngestor needs to
link against already exist -- proving the cross-ingestor id scheme actually matches,
not just that each ingestor works in isolation.
"""

from __future__ import annotations

from auryn.core.graph_client import GraphWriter, semantic_label
from auryn.ingestors.plsql.config import PLSQLSourceConfig
from auryn.ingestors.plsql.ingestor import PLSQLIngestor
from auryn.ingestors.sql.config import SQLSourceConfig
from auryn.ingestors.sql.ingestor import SQLIngestor


def _seed(oracle_schema, graph):
    SQLIngestor(SQLSourceConfig(dsn=oracle_schema["sqlalchemy_dsn"], database_name="orcltest")).run(
        GraphWriter(graph)
    )
    stats = PLSQLIngestor(
        PLSQLSourceConfig(dsn=oracle_schema["oracledb_dsn"], database_name="orcltest")
    ).run(GraphWriter(graph))
    return stats


def test_plsql_ingestor_creates_expected_graph(oracle_schema, graph):
    _seed(oracle_schema, graph)

    # RETURN the *semantic* label, not labels(n)[0]: with the system :Entity label present,
    # labels(n)[0] is order-dependent and returns "Entity" for any node whose own label was
    # registered after :Entity -- which is exactly the case here, since _seed runs the SQL
    # ingestor (registering :Entity) before PL/SQL registers Package/Procedure/... See
    # graph_client.semantic_label.
    counts = {
        row[0]: row[1]
        for row in graph.query(f"MATCH (n) RETURN {semantic_label('n')}, count(n)").result_set
    }
    assert counts["Package"] == 2  # greetings_pkg + wrapped secret_pkg
    assert counts["Procedure"] == 6  # log_greeting, greet, greet_all, notify x2, wrapped do_secret
    assert counts["Function"] == 3  # greet_count, double_it, wrapped secret_double
    assert counts["Trigger"] == 1

    # The cross-ingestor link: HAS_TRIGGER must land on the Table SQLIngestor created.
    trigger_rows = graph.query(
        """
        MATCH (t:Table)-[:HAS_TRIGGER]->(trig:Trigger)
        RETURN t.id, trig.name, trig.timing
        """
    ).result_set
    assert trigger_rows == [
        ["oracle://orcltest.test_plsql.employees", "TRG_EMPLOYEES_BIU", "BEFORE EACH ROW"]
    ]

    # CALLS/READS/WRITES: fine-grained (parsed) detail, not just the coarse
    # catalog-derived DEPENDS_ON signal.
    reads = {
        (row[0], row[1], row[2], row[3])
        for row in graph.query(
            f"MATCH (a)-[:READS]->(b) "
            f"RETURN {semantic_label('a')}, a.name, {semantic_label('b')}, b.name"
        ).result_set
    }
    writes = {
        (row[0], row[1], row[2], row[3], row[4])
        for row in graph.query(
            f"MATCH (a)-[r:WRITES]->(b) "
            f"RETURN {semantic_label('a')}, a.name, r.operation, {semantic_label('b')}, b.name"
        ).result_set
    }
    calls = {
        (row[0], row[1], row[2], row[3])
        for row in graph.query(
            f"MATCH (a)-[:CALLS]->(b) "
            f"RETURN {semantic_label('a')}, a.name, {semantic_label('b')}, b.name"
        ).result_set
    }
    assert ("Function", "GREET_COUNT", "Table", "employees") in reads
    assert ("Procedure", "LOG_GREETING", "UPDATE", "Table", "employees") in writes
    assert ("Procedure", "GREET", "Procedure", "LOG_GREETING") in calls
    assert ("Package", "GREETINGS_PKG", "Procedure", "GREET") in calls

    function_rows = graph.query(
        "MATCH (f:Function {name: 'GREET_COUNT'}) RETURN f.return_type, f.valid"
    ).result_set
    assert function_rows == [["NUMBER", True]]


def test_plsql_ingestor_models_overloads_distinctly(oracle_schema, graph):
    _seed(oracle_schema, graph)

    notify_rows = graph.query(
        """
        MATCH (:Package {name: 'GREETINGS_PKG'})-[:HAS_PROCEDURE]->(p:Procedure {name: 'NOTIFY'})
        -[:HAS_PARAMETER]->(param:Parameter)
        RETURN p.id, param.type
        ORDER BY param.type
        """
    ).result_set
    # Two distinct Procedure nodes (one per overload), not one node with two params.
    ids = {row[0] for row in notify_rows}
    assert len(ids) == 2
    assert {row[1] for row in notify_rows} == {"VARCHAR2", "NUMBER"}


def test_plsql_ingestor_gives_packaged_routines_their_own_source(oracle_schema, graph):
    _seed(oracle_schema, graph)

    # A packaged routine's own source -- not null, not the whole package body, just
    # its own declaration (ALL_SOURCE only gives the whole package body as one blob;
    # source_analysis.extract_package_member_sources() is what slices this out).
    greet_all_source = graph.query(
        "MATCH (p:Procedure {name: 'GREET_ALL'}) RETURN p.source"
    ).result_set[0][0]
    assert greet_all_source.strip().upper().startswith("PROCEDURE GREET_ALL")
    assert "double_it" not in greet_all_source.lower()

    double_it_source = graph.query(
        "MATCH (f:Function {name: 'DOUBLE_IT'}) RETURN f.source"
    ).result_set[0][0]
    assert double_it_source.strip().upper().startswith("FUNCTION DOUBLE_IT")
    assert "greet_all" not in double_it_source.lower()

    # The overloaded NOTIFY procedures must each get their OWN matching source, not
    # each other's or a blank -- this is the part that needs (name, param types)
    # matching rather than name alone.
    notify_sources = {
        row[0]: row[1]
        for row in graph.query(
            """
            MATCH (:Package {name: 'GREETINGS_PKG'})-[:HAS_PROCEDURE]->(p:Procedure {name: 'NOTIFY'})
            -[:HAS_PARAMETER]->(param:Parameter)
            RETURN param.type, p.source
            """
        ).result_set
    }
    assert "p_name" in notify_sources["VARCHAR2"].lower()
    assert "p_id" not in notify_sources["VARCHAR2"].lower()
    assert "p_id" in notify_sources["NUMBER"].lower()
    assert "p_name" not in notify_sources["NUMBER"].lower()

    # Standalone routines are unaffected by any of this -- they were never null.
    greet_source = graph.query("MATCH (p:Procedure {name: 'GREET'}) RETURN p.source").result_set[0][0]
    assert "log_greeting" in greet_source.lower()

    # The Package node itself still carries the full, unsliced body.
    body_source = graph.query(
        "MATCH (pkg:Package {name: 'GREETINGS_PKG'}) RETURN pkg.body_source"
    ).result_set[0][0]
    assert "greet_all" in body_source.lower()
    assert "double_it" in body_source.lower()
    assert "notify" in body_source.lower()


def test_plsql_ingestor_flags_wrapped_units_and_withholds_bytecode(oracle_schema, graph):
    _seed(oracle_schema, graph)

    # A `wrap`-obfuscated standalone function: the node still exists with its full
    # signature (ALL_PROCEDURES/ALL_ARGUMENTS survive wrapping) but is flagged wrapped
    # and its `source` is null -- never the obfuscated bytecode.
    secret_fn = graph.query(
        "MATCH (f:Function {name: 'SECRET_DOUBLE'}) RETURN f.wrapped, f.source, f.return_type"
    ).result_set
    assert secret_fn == [[True, None, "NUMBER"]]

    # ...and its parameter is still modeled, from the catalog rather than source.
    secret_fn_param = graph.query(
        """
        MATCH (:Function {name: 'SECRET_DOUBLE'})-[:HAS_PARAMETER]->(p:Parameter)
        RETURN p.name, p.type
        """
    ).result_set
    assert secret_fn_param == [["P", "NUMBER"]]

    # A wrapped package: both spec and body flagged, neither source stored.
    secret_pkg = graph.query(
        """
        MATCH (pkg:Package {name: 'SECRET_PKG'})
        RETURN pkg.spec_wrapped, pkg.spec_source, pkg.body_wrapped, pkg.body_source
        """
    ).result_set
    assert secret_pkg == [[True, None, True, None]]

    # Its member exists (signature from the catalog) but is flagged wrapped, no source.
    do_secret = graph.query(
        """
        MATCH (:Package {name: 'SECRET_PKG'})-[:HAS_PROCEDURE]->(p:Procedure {name: 'DO_SECRET'})
        RETURN p.wrapped, p.source
        """
    ).result_set
    assert do_secret == [[True, None]]

    # Readable units are flagged not-wrapped and keep their source intact -- the flag
    # distinguishes the vendor-wrapped core from the readable layer, not everything.
    readable = graph.query(
        """
        MATCH (pkg:Package {name: 'GREETINGS_PKG'})
        RETURN pkg.body_wrapped, pkg.spec_wrapped
        """
    ).result_set
    assert readable == [[False, False]]
    greet = graph.query("MATCH (p:Procedure {name: 'GREET'}) RETURN p.wrapped").result_set
    assert greet == [[False]]


def test_plsql_ingestor_is_idempotent(oracle_schema, graph):
    _seed(oracle_schema, graph)
    counts_after_first_run = (
        graph.query("MATCH (n) RETURN count(n)").result_set[0][0],
        graph.query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0],
    )

    PLSQLIngestor(PLSQLSourceConfig(dsn=oracle_schema["oracledb_dsn"], database_name="orcltest")).run(
        GraphWriter(graph)
    )
    counts_after_second_run = (
        graph.query("MATCH (n) RETURN count(n)").result_set[0][0],
        graph.query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0],
    )

    assert counts_after_first_run == counts_after_second_run


def test_plsql_ingestor_warns_when_database_node_is_missing(oracle_schema, graph, capsys):
    writer = GraphWriter(graph)
    ingestor = PLSQLIngestor(
        PLSQLSourceConfig(dsn=oracle_schema["oracledb_dsn"], database_name="never_ingested")
    )

    ingestor._warn_if_database_missing(writer)

    captured = capsys.readouterr()
    assert "no Database node found" in captured.err
    assert "never_ingested" in captured.err
