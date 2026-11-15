"""Integration tests for the file-based PL/SQL ingestor -- both entrypoints (`run
plsql` on a local directory and `git-sync plsql` on a repo) against the real `graph`
fixture. No Oracle needed: everything comes from parsing source files.
"""

from __future__ import annotations

import copy

import git

from auryn.core.graph_client import GraphWriter
from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.ingestors.plsql import resolution
from auryn.ingestors.plsql.config import GitPLSQLProjectConfig, PLSQLFileConfig
from auryn.ingestors.plsql.file_ingestor import PLSQLFileIngestor
from auryn.ingestors.plsql.git_sync import sync
from auryn.ingestors.plsql.raw_model import (
    RawColumn,
    RawIndexDef,
    RawPackage,
    RawParameter,
    RawPlsqlFile,
    RawRoutine,
    RawSequence,
    RawTable,
    RawTableAlteration,
    RawTrigger,
    RawType,
    RawView,
)

_SPEC = """
CREATE OR REPLACE PACKAGE hr IS
  PROCEDURE hire(p_name IN VARCHAR2);
  FUNCTION headcount RETURN NUMBER;
END hr;
"""

_BODY = """
CREATE OR REPLACE PACKAGE BODY hr IS
  PROCEDURE hire(p_name IN VARCHAR2) IS
  BEGIN
    INSERT INTO employees(name) VALUES (p_name);
    audit('hire');
  END;
  FUNCTION headcount RETURN NUMBER IS
    v NUMBER;
  BEGIN
    SELECT COUNT(*) INTO v FROM employees;
    RETURN v;
  END;
END hr;
"""

_AUDIT = """
CREATE OR REPLACE PROCEDURE audit(p_msg IN VARCHAR2) IS
BEGIN
  INSERT INTO audit_log(msg) VALUES (p_msg);
END;
"""


def _write_sources(root):
    (root / "hr.pks").write_text(_SPEC)
    (root / "hr.pkb").write_text(_BODY)
    (root / "audit.prc").write_text(_AUDIT)


def test_run_plsql_on_a_local_directory_builds_the_expected_subgraph(graph, tmp_path):
    _write_sources(tmp_path)

    stats = PLSQLFileIngestor(PLSQLFileConfig(path=str(tmp_path), project_name="HR", workers=1)).run(GraphWriter(graph))

    assert stats.nodes_merged > 0
    # Spec (.pks) and body (.pkb) collapse into ONE Package node, carrying both sources.
    pkg = graph.query(
        "MATCH (p:Package {id: 'plsql://HR.package.hr'}) RETURN p.spec_source IS NOT NULL, p.body_source IS NOT NULL"
    ).result_set
    assert pkg == [[True, True]]
    # Packaged members are attached to the package.
    assert graph.query(
        "MATCH (:Package {id:'plsql://HR.package.hr'})-[:HAS_PROCEDURE]->(p:Procedure {name:'HIRE'}) RETURN p"
    ).result_set != []
    # A packaged procedure's call resolves to the standalone procedure it invokes.
    assert graph.query(
        "MATCH (:Procedure {name:'HIRE'})-[:CALLS]->(a:Procedure {name:'AUDIT'}) RETURN a"
    ).result_set != []
    # READS/WRITES land on by-name Table endpoints rooted under the project.
    assert graph.query(
        "MATCH (:Procedure {name:'HIRE'})-[w:WRITES]->(t:Table {id:'plsql://HR.table.employees'}) RETURN w.operation"
    ).result_set == [["INSERT"]]
    assert graph.query(
        "MATCH (:Function {name:'HEADCOUNT'})-[:READS]->(t:Table {name:'EMPLOYEES'}) RETURN t"
    ).result_set != []


def test_run_plsql_logs_which_parser_engine_is_used(graph, tmp_path, caplog):
    # A run must make it unambiguous in the logs whether the fast C++ accelerator or the
    # pure-Python fallback did the parsing -- so a slow run can be diagnosed at a glance.
    import logging

    from auryn.ingestors.plsql.source_analysis import accelerator_available

    _write_sources(tmp_path)
    with caplog.at_level(logging.INFO, logger="auryn.ingestors.plsql.file_ingestor"):
        PLSQLFileIngestor(PLSQLFileConfig(path=str(tmp_path), project_name="HR", workers=1)).run(GraphWriter(graph))

    engine_lines = [r.getMessage() for r in caplog.records if "PL/SQL parser engine:" in r.getMessage()]
    assert len(engine_lines) == 1
    expected = "C++ accelerator" if accelerator_available() else "pure-Python ANTLR"
    assert expected in engine_lines[0]


def test_git_sync_plsql_first_sync_records_commit_and_ingests(graph, tmp_path):
    origin = tmp_path / "origin"
    repo = git.Repo.init(str(origin), initial_branch="main")
    with repo.config_writer() as cw:
        cw.set_value("user", "email", "t@e.com")
        cw.set_value("user", "name", "T")
    (origin / "db").mkdir(parents=True)
    _write_sources(origin / "db")
    repo.index.add(["db/hr.pks", "db/hr.pkb", "db/audit.prc"])
    commit = repo.index.commit("init plsql").hexsha

    config = GitPLSQLProjectConfig(repo_uri=str(origin), project_name="HR", workers=1)
    stats = sync(GraphWriter(graph), config, ref=commit, cache_root=tmp_path / "cache")

    assert stats.commit_sha == commit
    assert stats.files_parsed == 3
    assert graph.query(
        "MATCH (p:Project {id:'plsql://HR'}) RETURN p.commit_sha"
    ).result_set == [[commit]]
    assert graph.query("MATCH (:Procedure {name:'HIRE'})-[:CALLS]->(:Procedure {name:'AUDIT'}) RETURN 1").result_set != []


_SCHEMA_SQL = """
CREATE TABLE departments (
  id NUMBER PRIMARY KEY,
  name VARCHAR2(50) NOT NULL
);
CREATE TABLE employees (
  id NUMBER PRIMARY KEY,
  name VARCHAR2(100) NOT NULL,
  dept_id NUMBER,
  salary NUMBER DEFAULT 0,
  CONSTRAINT ck_sal CHECK (salary >= 0),
  CONSTRAINT fk_dept FOREIGN KEY (dept_id) REFERENCES departments(id)
);
CREATE OR REPLACE VIEW active_emps AS SELECT id, name FROM employees WHERE salary > 0;
CREATE INDEX idx_emp_dept ON employees(dept_id);
CREATE SEQUENCE emp_seq START WITH 1;
"""

_HIRE_SQL = """
CREATE OR REPLACE PROCEDURE hire(p_name IN VARCHAR2, p_dept IN NUMBER) IS
BEGIN
  INSERT INTO employees(name, dept_id) VALUES (p_name, p_dept);
END;
"""


def test_run_plsql_ingests_sql_ddl_entities(graph, tmp_path):
    (tmp_path / "schema.sql").write_text(_SCHEMA_SQL)

    PLSQLFileIngestor(PLSQLFileConfig(path=str(tmp_path), project_name="HR", workers=1)).run(GraphWriter(graph))

    # Tables + columns with their types/flags.
    assert {r[0] for r in graph.query("MATCH (t:Table) RETURN t.name").result_set} == {"DEPARTMENTS", "EMPLOYEES"}
    assert graph.query(
        "MATCH (:Table {id:'plsql://HR.table.employees'})-[:HAS_COLUMN]->(c:Column) RETURN c.name ORDER BY c.ordinal_position"
    ).result_set == [["ID"], ["NAME"], ["DEPT_ID"], ["SALARY"]]
    assert graph.query(
        "MATCH (c:Column {id:'plsql://HR.table.employees.salary'}) RETURN c.data_type, c.default_value, c.nullable"
    ).result_set == [["NUMBER", "0", True]]
    assert graph.query("MATCH (c:Column {id:'plsql://HR.table.employees.id'}) RETURN c.is_primary_key").result_set == [[True]]
    # Foreign key as a column->column REFERENCES edge.
    assert graph.query(
        "MATCH (:Column {id:'plsql://HR.table.employees.dept_id'})-[:REFERENCES]->(c:Column {id:'plsql://HR.table.departments.id'}) RETURN c"
    ).result_set != []
    # CHECK constraint, view, index, sequence all present.
    assert graph.query("MATCH (:Table {name:'EMPLOYEES'})-[:HAS_CONSTRAINT]->(k:Constraint {type:'CHECK'}) RETURN k").result_set != []
    assert graph.query("MATCH (:View {name:'ACTIVE_EMPS'})-[:READS]->(t:Table {name:'EMPLOYEES'}) RETURN t").result_set != []
    assert graph.query("MATCH (:Table {name:'EMPLOYEES'})-[:HAS_INDEX]->(i:Index)-[:INDEXES]->(c:Column {name:'DEPT_ID'}) RETURN i").result_set != []
    assert graph.query("MATCH (s:Sequence {name:'EMP_SEQ'}) RETURN s").result_set != []


_ALTER_SCHEMA_SQL = """
CREATE TABLE departments (
  id NUMBER PRIMARY KEY,
  name VARCHAR2(50) NOT NULL
);
CREATE TABLE employees (
  id NUMBER PRIMARY KEY,
  name VARCHAR2(100) NOT NULL
);
ALTER TABLE employees ADD (dept_id NUMBER, salary NUMBER DEFAULT 0);
ALTER TABLE employees ADD CONSTRAINT fk_emp_dept FOREIGN KEY (dept_id) REFERENCES departments(id);
"""


def test_run_plsql_applies_alter_table_from_files(graph, tmp_path):
    # A schema split across CREATE + ALTER (the common Oracle layout) reconstructs
    # fully from files: the ALTER-added column and its foreign key both land.
    (tmp_path / "schema.sql").write_text(_ALTER_SCHEMA_SQL)

    PLSQLFileIngestor(PLSQLFileConfig(path=str(tmp_path), project_name="HR", workers=1)).run(GraphWriter(graph))

    assert graph.query(
        "MATCH (:Table {id:'plsql://HR.table.employees'})-[:HAS_COLUMN]->(c:Column {name:'DEPT_ID'}) RETURN c"
    ).result_set != []
    assert graph.query(
        "MATCH (:Table {name:'EMPLOYEES'})-[:HAS_COLUMN]->(:Column {name:'DEPT_ID'})"
        "-[:REFERENCES]->(:Column {name:'ID'})<-[:HAS_COLUMN]-(:Table {name:'DEPARTMENTS'}) RETURN 1"
    ).result_set == [[1]]


def test_run_plsql_links_procedure_to_the_real_ddl_table(graph, tmp_path):
    # The payoff of unifying DDL + PL/SQL: the procedure writes the ACTUAL table node
    # (with columns), not a name-only stub.
    (tmp_path / "schema.sql").write_text(_SCHEMA_SQL)
    (tmp_path / "hire.prc").write_text(_HIRE_SQL)

    PLSQLFileIngestor(PLSQLFileConfig(path=str(tmp_path), project_name="HR", workers=1)).run(GraphWriter(graph))

    row = graph.query(
        "MATCH (:Procedure {name:'HIRE'})-[w:WRITES]->(t:Table {id:'plsql://HR.table.employees'}) "
        "OPTIONAL MATCH (t)-[:HAS_COLUMN]->(c:Column) "
        "RETURN w.operation, t.reference_only, count(c)"
    ).result_set
    assert row == [["INSERT", None, 4]]  # real table (not a stub), with its 4 columns


def test_git_sync_plsql_resync_same_ref_is_a_noop(graph, tmp_path):
    origin = tmp_path / "origin"
    repo = git.Repo.init(str(origin), initial_branch="main")
    with repo.config_writer() as cw:
        cw.set_value("user", "email", "t@e.com")
        cw.set_value("user", "name", "T")
    (origin / "audit.prc").write_text(_AUDIT)
    repo.index.add(["audit.prc"])
    commit = repo.index.commit("one proc").hexsha

    config = GitPLSQLProjectConfig(repo_uri=str(origin), project_name="HR", workers=1)
    sync(GraphWriter(graph), config, ref=commit, cache_root=tmp_path / "cache")
    again = sync(GraphWriter(graph), config, ref=commit, cache_root=tmp_path / "cache")

    assert again.nodes_added == again.nodes_removed == again.edges_added == 0
    assert again.files_parsed == 0  # blob-sha parse cache reused everything


# -- --files-from: ingest an explicit list instead of scanning by suffix -------------

_ODD_EXT_FUNCTION = """
CREATE OR REPLACE FUNCTION calc_total(p IN NUMBER) RETURN NUMBER IS
BEGIN
  RETURN p * 2;
END;
"""

_DECOY_FUNCTION = """
CREATE OR REPLACE FUNCTION secret_fn RETURN NUMBER IS
BEGIN
  RETURN 42;
END;
"""


def test_files_from_ingests_exact_list_and_bypasses_suffix_filter(graph, tmp_path):
    (tmp_path / "hr.pks").write_text(_SPEC)
    (tmp_path / "calc.txt").write_text(_ODD_EXT_FUNCTION)  # non-PLSQL suffix -> a scan would miss it
    (tmp_path / "decoy.fnc").write_text(_DECOY_FUNCTION)  # PLSQL suffix, but NOT in the list

    listed = (str(tmp_path / "hr.pks"), str(tmp_path / "calc.txt"))
    PLSQLFileIngestor(
        PLSQLFileConfig(path=str(tmp_path), project_name="X", workers=1, strict=False, files=listed)
    ).run(GraphWriter(graph))

    # The listed package (standard suffix) is ingested.
    assert graph.query("MATCH (p:Package {id:'plsql://X.package.hr'}) RETURN p").result_set != []
    # The function in the .txt file is ingested too -- the suffix filter is bypassed for an explicit list.
    assert graph.query("MATCH (f:Function {id:'plsql://X.function.calc_total'}) RETURN f").result_set != []
    # The decoy .fnc -- a standard suffix a directory scan WOULD pick up -- is absent: only the list ran.
    assert graph.query("MATCH (f:Function {name:'SECRET_FN'}) RETURN f").result_set == []


def test_files_from_skips_missing_and_outside_root(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    inside = root / "a.pks"
    inside.write_text(_SPEC)
    outside = tmp_path / "elsewhere.pks"  # exists, but not under the id root
    outside.write_text(_SPEC)

    config = PLSQLFileConfig(
        path=str(root),
        project_name="X",
        files=(str(inside), str(outside), str(root / "does_not_exist.pks")),
    )
    resolved = PLSQLFileIngestor(config)._resolve_explicit_files(root.resolve())

    assert resolved == [inside.resolve()]  # missing + outside-root entries dropped, not fatal


# -- Streaming resolution (recommendation B): build_plan + emit_streaming must be a
#    memory-bounded, byte-for-byte equivalent of resolve() over the whole corpus. --


def _param(name, dt):
    return RawParameter(name=name, position=1, direction="IN", data_type=dt, has_default=False)


def _routine(name, kind, params, source, calls=(), table_refs=()):
    return RawRoutine(name=name, kind=kind, parameters=list(params), source=source,
                      calls=list(calls), table_refs=list(table_refs))


def _cross_file_corpus():
    """A corpus exercising every case emit_streaming handles specially: a package split
    across spec (.pks) + body (.pkb), an overloaded packaged member, a table split across
    CREATE + ALTER (with an added column + FK), overloaded standalones in separate files,
    a view/type each defined twice (dedup), plus a trigger/index/sequence + a stub table."""
    return [
        RawPlsqlFile(path="hr.pks", packages=[RawPackage("HR", spec_source="SPEC", members=[
            _routine("LOG", "PROCEDURE", [_param("a", "NUMBER")], None),
            _routine("LOG", "PROCEDURE", [_param("a", "VARCHAR2")], None)])]),
        RawPlsqlFile(path="hr.pkb", packages=[RawPackage("HR", body_source="BODY", members=[
            _routine("LOG", "PROCEDURE", [_param("a", "NUMBER")], "b1", calls=["CALC", "CALC"], table_refs=[("audit", "INSERT")]),
            _routine("LOG", "PROCEDURE", [_param("a", "VARCHAR2")], "b2", calls=["CALC"], table_refs=[("audit", "SELECT")])])]),
        RawPlsqlFile(path="emp.sql", tables=[RawTable("EMP", columns=[RawColumn("id", 1, "NUMBER", False, None, True)])]),
        RawPlsqlFile(path="alter.sql", tables=[RawTable("DEPT", columns=[RawColumn("id", 1, "NUMBER", False, None, True)])],
                     alterations=[RawTableAlteration("EMP", columns=[RawColumn("dept_id", 0, "NUMBER", True, None, False)])]),
        RawPlsqlFile(path="calc1.fnc", routines=[_routine("CALC", "FUNCTION", [_param("x", "NUMBER")], "c1", table_refs=[("emp", "SELECT")])]),
        RawPlsqlFile(path="calc2.fnc", routines=[_routine("CALC", "FUNCTION", [_param("x", "VARCHAR2")], "c2", calls=["LOG"])]),
        RawPlsqlFile(path="v.sql", views=[RawView("V1", "SELECT 1", reads=["emp", "emp"])], types=[RawType("T1", "OBJECT")],
                     triggers=[RawTrigger("TRG", "emp", "t", calls=["CALC"], table_refs=[("audit", "INSERT")])],
                     indexes=[RawIndexDef("ix", "EMP", ["id"], False)], sequences=[RawSequence("S1")]),
        RawPlsqlFile(path="v2.sql", views=[RawView("V1", "SELECT 2", reads=["dept"])], types=[RawType("T1", "TABLE")]),
    ]


def _as_multisets(items):
    nodes: dict = {}
    edges: dict = {}
    for it in items:
        if isinstance(it, GraphNode):
            k = (it.label, it.key["id"], tuple(sorted((p, repr(v)) for p, v in it.properties.items())))
            nodes[k] = nodes.get(k, 0) + 1
        else:
            k = (it.rel_type, it.from_key["id"], it.to_key["id"],
                 tuple(sorted((p, repr(v)) for p, v in it.properties.items())))
            edges[k] = edges.get(k, 0) + 1
    return nodes, edges


def test_emit_streaming_matches_resolve_including_cross_file_entities():
    raw = _cross_file_corpus()
    proj = "plsql://P"

    # resolve() and _apply_alterations both mutate their inputs, so give each path its own
    # deep copies -- exactly what git_sync gets by re-loading fresh from the parse cache.
    a_nodes, a_edges = _as_multisets(resolution.resolve([copy.deepcopy(f) for f in raw], proj))

    snapshot = {i: f for i, f in enumerate(raw)}
    refs = [(f.path, i) for i, f in enumerate(raw)]

    def load_fn(sha):  # fresh copy per call (see resolution.build_plan docstring)
        return copy.deepcopy(snapshot[sha])

    plan = resolution.build_plan(refs, load_fn, proj)
    b_nodes, b_edges = _as_multisets(resolution.emit_streaming(plan, load_fn, proj))

    assert b_nodes == a_nodes
    assert b_edges == a_edges
    # sanity: the corpus actually produced the tricky entities, not an empty match
    assert any(lbl == "Column" and nid.endswith("emp.dept_id") for (lbl, nid, _) in a_nodes)  # ALTER column present
    assert any(lbl == "Package" and nid.endswith("package.hr") for (lbl, nid, _) in a_nodes)  # spec+body merged
