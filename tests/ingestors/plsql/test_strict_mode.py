"""Tests for the strict / fail-fast SQL ingestion mode and its diagnostics (the mode used for
a first ingestion of unfamiliar prospect data). Covers: strict parse raises a positioned error
instead of swallowing; the file ingestor halts on the first bad file, writes a full diagnostic,
and leaves the graph empty; dialect signals identify non-Oracle input; strict still succeeds on
valid Oracle DDL; and coverage gaps (parsed-but-not-modelled statements) are surfaced.
"""

from __future__ import annotations

import json
import logging

import pytest

from auryn.core.graph_client import GraphWriter
from auryn.ingestors.plsql import diagnostics
from auryn.ingestors.plsql.config import PLSQLFileConfig
from auryn.ingestors.plsql.file_ingestor import PLSQLFileIngestor
from auryn.ingestors.plsql.source_analysis import parse_declarations, unmodeled_statement_kinds

_GOOD = "CREATE TABLE departments (id NUMBER PRIMARY KEY, name VARCHAR2(50) NOT NULL);\n"
_BAD_ORACLE = "CREATE TABLE employees (\n  id NUMBER,\n  bad @#$ column,\n  name VARCHAR2(100)\n);\n"
_TSQL = (
    "CREATE TABLE [dbo].[Users] (\n"
    "  Id INT IDENTITY(1,1) PRIMARY KEY,\n"
    "  Name NVARCHAR(50) NOT NULL\n"
    ");\nGO\n"
)


# -- parse-level (no FalkorDB) -------------------------------------------------------------

def test_strict_parse_raises_positioned_error():
    with pytest.raises(diagnostics.SqlSyntaxError) as excinfo:
        parse_declarations(_BAD_ORACLE, "employees.sql", strict=True)
    error = excinfo.value
    assert error.line == 3  # the `bad @#$ column` line
    assert error.offending_token is not None


def test_non_strict_recovers_and_continues_without_raising():
    # Best-effort (non-strict) recovers from a syntax/grammar error instead of raising OR
    # dropping the whole file: it skips the offending construct, keeps parsing, and records how
    # many it skipped (parse_errors) so the gap is visible.
    result = parse_declarations(_BAD_ORACLE, "employees.sql")
    assert result.parse_errors > 0  # the bad column was recovered-from, not raised on


def test_unmodeled_statement_kinds_are_reported():
    source = (
        "CREATE TABLE t (id NUMBER PRIMARY KEY);\n"
        "CREATE MATERIALIZED VIEW mv AS SELECT * FROM t;\n"
        "CREATE SYNONYM syn FOR t;\n"
        "COMMENT ON TABLE t IS 'x';\n"
    )
    raw = parse_declarations(source, "t.sql", strict=True)
    assert raw.tables and raw.tables[0].name == "T"  # modelled
    assert set(unmodeled_statement_kinds(raw)) == {
        "Create_materialized_view", "Create_synonym", "Comment_on_table",
    }


# -- auryn-level (real graph fixture) ---------------------------------------------------

def test_strict_run_halts_writes_diagnostic_and_leaves_graph_empty(graph, tmp_path):
    (tmp_path / "a_ok.sql").write_text(_GOOD)
    (tmp_path / "b_bad.sql").write_text(_BAD_ORACLE)
    diag_dir = tmp_path / "diagnostics"

    config = PLSQLFileConfig(
        path=str(tmp_path), project_name="P", strict=True, diagnostics_dir=str(diag_dir)
    )
    with pytest.raises(diagnostics.StrictHaltError) as excinfo:
        PLSQLFileIngestor(config).run(GraphWriter(graph))

    halt = excinfo.value
    assert "b_bad.sql" in str(halt)
    # A diagnostic report was written, machine- and human-readable.
    assert halt.report_path.exists() and halt.report_path.with_suffix(".txt").exists()
    report = json.loads(halt.report_path.read_text())
    assert report["kind"] == "parse_error"
    assert report["file"] == "b_bad.sql"
    assert report["error"]["line"] == 3
    assert report["source_window"] and ">>" in report["source_window"]
    # It got through the good file first -- progress records that.
    assert report["progress"]["files_parsed_ok"] == 1
    # Halt happened before any graph write: nothing landed.
    assert graph.query("MATCH (n) RETURN count(n)").result_set == [[0]]


def test_strict_diagnostic_identifies_non_oracle_dialect(graph, tmp_path):
    (tmp_path / "users.sql").write_text(_TSQL)
    diag_dir = tmp_path / "diagnostics"

    config = PLSQLFileConfig(
        path=str(tmp_path), project_name="P", strict=True, diagnostics_dir=str(diag_dir)
    )
    with pytest.raises(diagnostics.StrictHaltError) as excinfo:
        PLSQLFileIngestor(config).run(GraphWriter(graph))

    report = json.loads(excinfo.value.report_path.read_text())
    assert report["likely_dialect"] == "tsql"
    assert "tsql" in report["dialect_signals"]
    assert "SQL Server" in report["hint"]


def test_strict_encoding_failure_halts_with_encoding_report(graph, tmp_path):
    (tmp_path / "latin.sql").write_bytes(b"CREATE TABLE t (name VARCHAR2(10)); -- \xff\xfe not utf8\n")
    diag_dir = tmp_path / "diagnostics"

    # Restrict to utf-8 only so the invalid bytes can't be salvaged by a fallback.
    config = PLSQLFileConfig(
        path=str(tmp_path), project_name="P", strict=True, diagnostics_dir=str(diag_dir), encodings=("utf-8",)
    )
    with pytest.raises(diagnostics.StrictHaltError) as excinfo:
        PLSQLFileIngestor(config).run(GraphWriter(graph))

    report = json.loads(excinfo.value.report_path.read_text())
    assert report["kind"] == "encoding_error"
    assert report["first_bad_byte"]["offset"] > 0


def test_strict_succeeds_on_valid_oracle_ddl(graph, tmp_path):
    (tmp_path / "schema.sql").write_text(
        "CREATE TABLE departments (id NUMBER PRIMARY KEY, name VARCHAR2(50) NOT NULL);\n"
        "CREATE OR REPLACE PROCEDURE noop IS BEGIN NULL; END;\n"
    )
    config = PLSQLFileConfig(path=str(tmp_path), project_name="P", strict=True, diagnostics_dir=str(tmp_path / "d"))

    stats = PLSQLFileIngestor(config).run(GraphWriter(graph))  # must not halt

    assert stats.nodes_merged > 0
    assert graph.query("MATCH (t:Table {name:'DEPARTMENTS'}) RETURN t").result_set != []


def test_strict_logs_coverage_gaps(graph, tmp_path, caplog):
    (tmp_path / "extra.sql").write_text(
        "CREATE TABLE t (id NUMBER PRIMARY KEY);\n"
        "CREATE MATERIALIZED VIEW mv AS SELECT * FROM t;\n"
        "CREATE SYNONYM syn FOR t;\n"
    )
    config = PLSQLFileConfig(path=str(tmp_path), project_name="P", strict=True, diagnostics_dir=str(tmp_path / "d"))

    with caplog.at_level(logging.WARNING, logger="auryn.ingestors.plsql.file_ingestor"):
        PLSQLFileIngestor(config).run(GraphWriter(graph))

    coverage_logs = [r.getMessage() for r in caplog.records if "COVERAGE GAP" in r.getMessage()]
    assert coverage_logs and "Create_materialized_view" in coverage_logs[0]
