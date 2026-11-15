"""Pure-Python unit tests for parse_unit()/extract_package_member_sources() -- no
Oracle needed, just the vendored ANTLR grammar against hand-written PL/SQL snippets."""

from __future__ import annotations

from auryn.ingestors.plsql.source_analysis import (
    ParsedUnit,
    extract_package_member_sources,
    parse_declarations,
    parse_unit,
)


def test_parse_unit_extracts_a_direct_procedure_call():
    source = """
    CREATE OR REPLACE PROCEDURE greet(p_name IN VARCHAR2) IS
    BEGIN
      log_greeting(p_name);
    END;
    """
    result = parse_unit(source)
    assert result.calls == ["LOG_GREETING"]
    assert result.table_refs == []


def test_parse_unit_extracts_an_update_target():
    source = """
    CREATE OR REPLACE PROCEDURE log_greeting(p_name IN VARCHAR2) IS
    BEGIN
      UPDATE employees SET last_greeted = SYSDATE WHERE name = p_name;
    END;
    """
    result = parse_unit(source)
    assert result.calls == []
    assert [(r.name, r.operation) for r in result.table_refs] == [("EMPLOYEES", "UPDATE")]


def test_parse_unit_extracts_a_select_target():
    source = """
    CREATE OR REPLACE FUNCTION greet_count(p_name IN VARCHAR2) RETURN NUMBER IS
      v_count NUMBER;
    BEGIN
      SELECT COUNT(*) INTO v_count FROM employees WHERE name = p_name;
      RETURN v_count;
    END;
    """
    result = parse_unit(source)
    assert [(r.name, r.operation) for r in result.table_refs] == [("EMPLOYEES", "SELECT")]


def test_parse_unit_extracts_an_insert_target():
    source = """
    CREATE OR REPLACE PROCEDURE add_employee(p_name VARCHAR2) IS
    BEGIN
      INSERT INTO employees (name) VALUES (p_name);
    END;
    """
    result = parse_unit(source)
    assert [(r.name, r.operation) for r in result.table_refs] == [("EMPLOYEES", "INSERT")]


def test_parse_unit_handles_a_bare_trigger_body_via_the_block_rule():
    body = "BEGIN :NEW.last_greeted := SYSDATE; END;"
    result = parse_unit(body, rule="block")
    assert result.calls == []
    assert result.table_refs == []


def test_parse_unit_handles_a_trigger_body_with_a_declare_section():
    # ALL_TRIGGERS.TRIGGER_BODY commonly starts with DECLARE for local variables --
    # the "body" rule alone rejects this (it only matches BEGIN...END), which is
    # exactly why triggers are parsed with rule="block" (optional DECLARE + body).
    body = """
    DECLARE
      v_min_salary NUMBER;
    BEGIN
      SELECT min_salary INTO v_min_salary FROM jobs WHERE job_id = :NEW.job_id;
    END;
    """
    result = parse_unit(body, rule="block")
    assert [(r.name, r.operation) for r in result.table_refs] == [("JOBS", "SELECT")]


def test_parse_unit_never_raises_on_invalid_source():
    # Non-strict recovers rather than raising; it may salvage partial best-effort content from
    # invalid source, but the contract is simply: it must not raise, and returns a ParsedUnit.
    result = parse_unit("this is not valid PL/SQL at all {{{")
    assert isinstance(result, ParsedUnit)


def test_extract_package_member_sources_slices_out_each_member():
    source = """
    CREATE OR REPLACE PACKAGE BODY greetings_pkg IS
        PROCEDURE greet_all IS
        BEGIN
          greet('all');
        END greet_all;
        FUNCTION double_it(p_n IN NUMBER) RETURN NUMBER IS
        BEGIN
          RETURN p_n * 2;
        END double_it;
    END greetings_pkg;
    """
    members = extract_package_member_sources(source)

    assert set(members) == {("GREET_ALL", ()), ("DOUBLE_IT", ("NUMBER",))}
    assert members[("GREET_ALL", ())].strip().startswith("PROCEDURE greet_all")
    assert members[("GREET_ALL", ())].strip().endswith("END greet_all;")
    assert "double_it" not in members[("GREET_ALL", ())].lower().split("procedure")[0]
    assert members[("DOUBLE_IT", ("NUMBER",))].strip().startswith("FUNCTION double_it")


def test_extract_package_member_sources_disambiguates_overloads_by_parameter_type():
    source = """
    CREATE OR REPLACE PACKAGE BODY greetings_pkg IS
        PROCEDURE notify(p_name IN VARCHAR2) IS
        BEGIN
          NULL;
        END notify;
        PROCEDURE notify(p_id IN NUMBER) IS
        BEGIN
          NULL;
        END notify;
    END greetings_pkg;
    """
    members = extract_package_member_sources(source)

    assert set(members) == {("NOTIFY", ("VARCHAR2",)), ("NOTIFY", ("NUMBER",))}
    assert "p_name" in members[("NOTIFY", ("VARCHAR2",))]
    assert "p_id" in members[("NOTIFY", ("NUMBER",))]


def test_extract_package_member_sources_never_raises_on_invalid_source():
    assert extract_package_member_sources("this is not valid PL/SQL at all {{{") == {}


# -- parse_declarations (file-based structural extraction) --------------------------


def test_parse_declarations_extracts_a_package_spec_and_body():
    source = """
    CREATE OR REPLACE PACKAGE greetings_pkg IS
      PROCEDURE greet_all;
      FUNCTION double_it(p_n IN NUMBER) RETURN NUMBER;
    END greetings_pkg;
    /
    CREATE OR REPLACE PACKAGE BODY greetings_pkg IS
      PROCEDURE greet_all IS BEGIN log_it('x'); END;
      FUNCTION double_it(p_n IN NUMBER) RETURN NUMBER IS BEGIN RETURN p_n * 2; END;
    END greetings_pkg;
    """
    result = parse_declarations(source, "greetings.sql")

    # Both a spec and a body unit for the same package (resolution merges them later).
    assert [(p.name, p.spec_source is not None, p.body_source is not None) for p in result.packages] == [
        ("GREETINGS_PKG", True, False),
        ("GREETINGS_PKG", False, True),
    ]
    body = next(p for p in result.packages if p.body_source)
    members = {(m.name, m.kind): m for m in body.members}
    assert set(members) == {("GREET_ALL", "PROCEDURE"), ("DOUBLE_IT", "FUNCTION")}
    assert members[("DOUBLE_IT", "FUNCTION")].return_type == "NUMBER"
    assert members[("DOUBLE_IT", "FUNCTION")].parameters[0].name == "p_n"
    assert members[("GREET_ALL", "PROCEDURE")].calls == ["LOG_IT"]


def test_parse_declarations_extracts_standalone_routines_with_parameter_directions():
    source = """
    CREATE OR REPLACE PROCEDURE log_greeting(p_name IN VARCHAR2, p_count OUT NUMBER) IS
    BEGIN
      UPDATE employees SET last_greeted = SYSDATE WHERE name = p_name;
    END;
    """
    result = parse_declarations(source, "log_greeting.prc")

    assert len(result.routines) == 1
    routine = result.routines[0]
    assert (routine.name, routine.kind) == ("LOG_GREETING", "PROCEDURE")
    assert [(p.name, p.direction, p.data_type) for p in routine.parameters] == [
        ("p_name", "IN", "VARCHAR2"),
        ("p_count", "OUT", "NUMBER"),
    ]
    assert routine.table_refs == [("EMPLOYEES", "UPDATE")]


def test_parse_declarations_extracts_a_trigger_with_its_table():
    source = """
    CREATE OR REPLACE TRIGGER trg_emp
    BEFORE INSERT OR UPDATE ON employees
    FOR EACH ROW
    BEGIN
      :NEW.last_greeted := SYSDATE;
    END;
    """
    result = parse_declarations(source, "trg_emp.trg")

    assert len(result.triggers) == 1
    assert (result.triggers[0].name, result.triggers[0].table) == ("TRG_EMP", "EMPLOYEES")


def test_parse_declarations_extracts_object_types():
    result = parse_declarations(
        "CREATE OR REPLACE TYPE addr_t AS OBJECT (street VARCHAR2(100), city VARCHAR2(50));", "addr_t.tps"
    )
    assert [(t.name, t.kind) for t in result.types] == [("ADDR_T", "OBJECT")]


def test_parse_declarations_never_raises_on_invalid_source():
    result = parse_declarations("this is not valid PL/SQL at all {{{", "junk.sql")
    assert result.is_empty()


# -- parse_declarations: SQL DDL ----------------------------------------------------


def test_parse_declarations_extracts_a_table_with_columns_and_constraints():
    source = """
    CREATE TABLE employees (
      id NUMBER PRIMARY KEY,
      name VARCHAR2(100) NOT NULL,
      salary NUMBER DEFAULT 0,
      dept_id NUMBER,
      CONSTRAINT uq_name UNIQUE (name),
      CONSTRAINT ck_sal CHECK (salary >= 0),
      CONSTRAINT fk_dept FOREIGN KEY (dept_id) REFERENCES departments(id)
    );
    """
    result = parse_declarations(source, "employees.sql")

    assert len(result.tables) == 1
    table = result.tables[0]
    assert table.name == "EMPLOYEES"
    cols = {c.name: c for c in table.columns}
    assert set(cols) == {"ID", "NAME", "SALARY", "DEPT_ID"}
    assert cols["ID"].is_primary_key is True and cols["ID"].data_type == "NUMBER"
    assert cols["NAME"].nullable is False
    assert cols["SALARY"].default_value == "0" and cols["SALARY"].nullable is True
    types = {c.type for c in table.constraints}
    assert types == {"PRIMARY_KEY", "UNIQUE", "CHECK"}
    assert len(table.foreign_keys) == 1
    fk = table.foreign_keys[0]
    assert (fk.columns, fk.ref_table, fk.ref_columns) == (["DEPT_ID"], "DEPARTMENTS", ["ID"])


def test_parse_declarations_extracts_view_index_and_sequence():
    source = """
    CREATE OR REPLACE VIEW active_emps AS SELECT id, name FROM employees WHERE salary > 0;
    CREATE UNIQUE INDEX idx_emp_name ON employees(name, id);
    CREATE SEQUENCE emp_seq START WITH 1;
    """
    result = parse_declarations(source, "schema.sql")

    assert [(v.name, v.reads) for v in result.views] == [("ACTIVE_EMPS", ["EMPLOYEES"])]
    assert [(i.name, i.table, i.columns, i.unique) for i in result.indexes] == [
        ("IDX_EMP_NAME", "EMPLOYEES", ["NAME", "ID"], True)
    ]
    assert [s.name for s in result.sequences] == ["EMP_SEQ"]


def test_parse_declarations_extracts_alter_table_additions():
    source = """
    CREATE TABLE employees (id NUMBER PRIMARY KEY, name VARCHAR2(100));
    ALTER TABLE employees ADD (dept_id NUMBER, salary NUMBER DEFAULT 0);
    ALTER TABLE employees ADD CONSTRAINT fk_emp_dept FOREIGN KEY (dept_id) REFERENCES departments(id);
    ALTER TABLE employees ADD CONSTRAINT uq_emp_name UNIQUE (name);
    """
    result = parse_declarations(source, "schema.sql")

    assert [t.name for t in result.tables] == ["EMPLOYEES"]
    added_columns = [c.name for a in result.alterations for c in a.columns]
    assert added_columns == ["DEPT_ID", "SALARY"]
    fks = [fk for a in result.alterations for fk in a.foreign_keys]
    assert [(fk.columns, fk.ref_table, fk.ref_columns) for fk in fks] == [(["DEPT_ID"], "DEPARTMENTS", ["ID"])]
    constraint_types = [k.type for a in result.alterations for k in a.constraints]
    assert "UNIQUE" in constraint_types


def test_parse_declarations_handles_ddl_and_plsql_in_one_file():
    source = """
    CREATE TABLE employees (id NUMBER PRIMARY KEY, name VARCHAR2(100));
    CREATE OR REPLACE PROCEDURE hire(p_name IN VARCHAR2) IS
    BEGIN INSERT INTO employees(name) VALUES (p_name); END;
    """
    result = parse_declarations(source, "mixed.sql")

    assert [t.name for t in result.tables] == ["EMPLOYEES"]
    assert [(r.name, r.table_refs) for r in result.routines] == [("HIRE", [("EMPLOYEES", "INSERT")])]


# -- SQL*Plus directive handling (Oracle deploy scripts) ----------------------------

def test_parse_declarations_skips_sqlplus_prompt_wrapped_ddl():
    # prompt/set/spool/@ etc. are SQL*Plus commands, not PL/SQL; blanked before parsing so the
    # real DDL still parses (regression for a real-world create_utility_tables.sql).
    source = (
        "prompt Creating table UTIL$VALUES ...\n"
        "CREATE TABLE UTIL$VALUES (id NUMBER(12) PRIMARY KEY, code VARCHAR2(4));\n"
        "\n"
        "prompt Creating index IDX1 ...\n"
        "CREATE INDEX IDX1 ON UTIL$VALUES (code);\n"
        "set define off\n"
        "@@some_other_script.sql\n"
    )
    result = parse_declarations(source, "create_utility_tables.sql", strict=True)  # must not raise
    assert [t.name for t in result.tables] == ["UTIL$VALUES"]
    assert [i.name for i in result.indexes] == ["IDX1"]


def test_sqlplus_stripping_leaves_update_set_intact():
    # `SET` at the start of a continuation line of an UPDATE must NOT be treated as SQL*Plus
    # `SET` (it has `=`), or the statement would break.
    source = (
        "CREATE OR REPLACE PROCEDURE bump IS\n"
        "BEGIN\n"
        "  UPDATE employees\n"
        "  SET salary = salary * 2\n"
        "  WHERE id = 1;\n"
        "END;\n/\n"
    )
    result = parse_declarations(source, "bump.prc", strict=True)
    (routine,) = result.routines
    assert routine.name == "BUMP"
    assert ("EMPLOYEES", "UPDATE") in routine.table_refs  # UPDATE survived intact


def test_sqlplus_blanking_preserves_offsets_for_diagnostics():
    from auryn.ingestors.plsql.source_analysis import _blank_sqlplus_directives

    source = "prompt hi there\nCREATE TABLE t (id NUMBER);\n"
    blanked = _blank_sqlplus_directives(source)
    assert len(blanked) == len(source)  # equal length -> byte offsets unchanged
    assert blanked.splitlines()[0].strip() == ""  # directive content gone
    assert "CREATE TABLE t" in blanked  # real DDL untouched


def test_sqlplus_stripping_does_not_touch_sql_clauses_that_start_with_directive_words():
    # Regression: `start`/`connect`/`exec`/`exit` are SQL*Plus commands BUT also begin real
    # PL/SQL clauses (START WITH / CONNECT BY / EXECUTE IMMEDIATE / EXIT WHEN). The stripper
    # must NOT blank those lines -- doing so silently broke a valid hierarchical query and
    # looked like a grammar gap.
    hierarchical = (
        "CREATE OR REPLACE FUNCTION f RETURN NUMBER IS n NUMBER;\n"
        "BEGIN\n"
        "  SELECT c INTO n FROM t\n"
        "  CONNECT BY PRIOR a = b\n"
        "  START WITH a IN (SELECT x FROM t2\n"
        "                   START WITH x = 1\n"
        "                   CONNECT BY PRIOR y = x);\n"
        "  RETURN n;\n"
        "END;\n/\n"
    )
    result = parse_declarations(hierarchical, "f.sql", strict=True)  # must not raise
    assert [r.name for r in result.routines] == ["F"]

    exec_immediate = (
        "CREATE OR REPLACE PROCEDURE p IS\nBEGIN\n"
        "  EXECUTE IMMEDIATE 'create table x (id number)';\n"
        "  FOR i IN 1..3 LOOP EXIT WHEN i > 2; END LOOP;\n"
        "  SET TRANSACTION READ ONLY;\nEND;\n/\n"
    )
    assert parse_declarations(exec_immediate, "p.sql", strict=True).routines[0].name == "P"


def test_sqlplus_set_only_strips_known_options_not_sql_set_statements():
    from auryn.ingestors.plsql.source_analysis import _blank_sqlplus_directives

    # `SET DEFINE OFF` (SQL*Plus) -> blanked; `SET TRANSACTION` / `SET col = 1` (SQL) -> kept.
    assert _blank_sqlplus_directives("SET DEFINE OFF\n").strip() == ""
    assert "SET TRANSACTION" in _blank_sqlplus_directives("SET TRANSACTION READ ONLY;\n")
    assert "SET x = 1" in _blank_sqlplus_directives("  SET x = 1\n")


def test_sqlplus_conditional_commands_blanked_but_sql_clauses_kept():
    from auryn.ingestors.plsql.source_analysis import _blank_sqlplus_directives as b

    # SQL*Plus command forms -> blanked
    assert b("start dropdatalayer\n").strip() == ""
    assert b("connect scott/tiger\n").strip() == ""
    assert b("execute some_proc\n").strip() == ""
    assert b("show errors;\n").strip() == ""
    # the SQL/PL-SQL clause forms of the same words -> kept
    assert "start with" in b("  start with x = 1\n")
    assert "connect by" in b("  connect by prior a = b\n")
    assert "execute immediate" in b("  execute immediate 'x'\n")


def test_datalayer_style_dropscript_parses():
    # regression for DataLayer.sql: SQL*Plus `show errors` / `start <script>` wrapping DROP +
    # CREATE TYPE ... AS OBJECT.
    source = (
        "show errors;\n\nstart dropdatalayer\nset feedback off\n\n"
        "drop package DataLayerXML;\n\ndrop type objError force;\n/\n\n"
        "prompt Creating type...\n"
        "create or replace type objError as object(x number,"
        " member function version return varchar2);\n/\n"
    )
    result = parse_declarations(source, "DataLayer.sql", strict=True)  # must not raise
    assert [t.name for t in result.types] == ["OBJERROR"]


def test_non_literal_default_is_neutralized_and_whole_file_extracts_cleanly():
    # A non-literal parameter default (`:= objUser()`) is neutralized before parsing, so the file
    # parses CLEANLY (no recovery needed) and every declaration comes through.
    source = (
        "create or replace type objComment as object(\n"
        "  id number,\n"
        "  constructor function objComment(p objUser := objUser()) return self as result,\n"
        "  member function version return varchar2\n"
        ");\n/\n"
        "create or replace type tblComments as table of objComment;\n/\n"
        "create or replace procedure keep_me is begin null; end;\n/\n"
    )
    result = parse_declarations(source, "objComment.sql")  # non-strict; must not raise
    assert result.parse_errors == 0  # neutralization fixed the gap -- nothing to recover from
    assert {t.name for t in result.types} == {"OBJCOMMENT", "TBLCOMMENTS"}
    assert [r.name for r in result.routines] == ["KEEP_ME"]


def test_best_effort_recovers_from_a_genuine_syntax_error_and_keeps_the_rest():
    # A construct the grammar genuinely can't parse (garbage tokens) is recovered-from, not
    # dropped: the file's other declarations still come through, and the gap is recorded.
    source = (
        "CREATE TABLE good1 (id NUMBER PRIMARY KEY);\n"
        "CREATE TABLE bad (id NUMBER, @#$ !! garbage, name VARCHAR2(10));\n"
        "CREATE OR REPLACE PROCEDURE keep_me IS BEGIN NULL; END;\n/\n"
    )
    result = parse_declarations(source, "mixed.sql")  # non-strict; must not raise
    assert result.parse_errors > 0
    assert "GOOD1" in {t.name for t in result.tables}
    assert [r.name for r in result.routines] == ["KEEP_ME"]


def test_best_effort_clean_file_has_no_parse_errors():
    result = parse_declarations("CREATE TABLE t (id NUMBER PRIMARY KEY);", "t.sql")
    assert result.parse_errors == 0 and result.first_error is None


def test_non_literal_parameter_defaults_are_neutralized_so_the_file_parses():
    # The vendored grammar only allows a literal `constant` as a parameter default, but Oracle
    # allows any expression. We neutralize the default *value* (not the parameter) so the file
    # parses in STRICT too, and body assignments are left alone. (Regression: DataLayer.sql
    # `pUserObject objUser := objUser()`.)
    source = (
        "create or replace type objComment as object(\n"
        "  id number,\n"
        "  constructor function objComment(p objUser := objUser(), n number := sysdate) return self as result,\n"
        "  member function version return varchar2\n"
        ");\n/\n"
    )
    result = parse_declarations(source, "DataLayer.sql", strict=True)  # must NOT raise
    assert [t.name for t in result.types] == ["OBJCOMMENT"]


def test_neutralize_defaults_leaves_body_assignments_and_updates_untouched():
    from auryn.ingestors.plsql.source_analysis import _neutralize_param_defaults as neutralize

    body = "CREATE OR REPLACE PROCEDURE p IS v NUMBER; BEGIN v := f(a, b); UPDATE t SET x = 1 WHERE id = 2; END;\n/"
    out = neutralize(body)
    assert ":= f(a, b)" in out  # body assignment (paren depth 0) untouched
    assert "SET x = 1" in out
    assert len(out) == len(body)  # length preserved -> offsets/diagnostics stay exact
    # a real parameter default IS neutralized
    assert neutralize("f(p number := g(x))").count(":=") == 1 and "g(x)" not in neutralize("f(p number := g(x))")


# -- Top-level INSERT (data-population) stripping -----------------------------------


def test_strip_top_level_inserts_blanks_data_and_preserves_offsets():
    from auryn.ingestors.plsql.source_analysis import _strip_top_level_inserts as strip

    source = (
        "SET DEFINE OFF;\n"
        "Insert into T (ID,NAME) values (1,'a;b');\n"  # ';' inside the string must not end it early
        "Insert into T (ID,NAME) values (2,'c');\n"
        "COMMIT;\n"
    )
    out = strip(source)
    assert len(out) == len(source)  # length preserved -> byte offsets unchanged
    assert out.count("\n") == source.count("\n")  # line-preserving -> diagnostics line numbers hold
    assert "insert" not in out.lower()  # every top-level INSERT gone
    assert "SET DEFINE OFF;" in out and "COMMIT;" in out  # surrounding statements untouched


def test_strip_keeps_schema_ddl_but_drops_seed_inserts():
    source = (
        "CREATE TABLE t (id NUMBER PRIMARY KEY, name VARCHAR2(100));\n"
        "INSERT INTO t (id, name) VALUES (1, 'seed');\n"
        "INSERT INTO t (id, name) VALUES (2, 'seed2');\n"
    )
    result = parse_declarations(source, "seed.sql")
    assert [t.name for t in result.tables] == ["T"]  # schema kept
    assert "Insert_statement" not in result.statement_kinds  # data INSERTs never reach the parser


def test_strip_preserves_inserts_inside_a_procedure_body():
    # The INSERT inside a body is application logic -> it must still become a table edge, while a
    # standalone top-level INSERT in the same file is stripped as data.
    source = (
        "CREATE OR REPLACE PROCEDURE hire(p_name IN VARCHAR2) IS\n"
        "BEGIN\n"
        "  INSERT INTO employees(name) VALUES (p_name);\n"
        "END;\n"
        "/\n"
        "INSERT INTO employees(name) VALUES ('data-only');\n"
    )
    result = parse_declarations(source, "hire.sql")
    assert [(r.name, r.table_refs) for r in result.routines] == [("HIRE", [("EMPLOYEES", "INSERT")])]


def test_strip_never_touches_a_trigger_header_insert_keyword():
    # `BEFORE INSERT` in a trigger header is not a statement leader -- stripping it would destroy
    # the trigger. The body INSERT (depth > 0) is also preserved.
    source = (
        "CREATE OR REPLACE TRIGGER emp_bi BEFORE INSERT OR UPDATE ON employees\n"
        "FOR EACH ROW\n"
        "BEGIN\n"
        "  INSERT INTO audit(msg) VALUES ('hired');\n"
        "END;\n"
        "/\n"
    )
    result = parse_declarations(source, "trg.trg")
    assert [(t.name, t.table) for t in result.triggers] == [("EMP_BI", "EMPLOYEES")]
    assert result.triggers[0].table_refs == [("AUDIT", "INSERT")]


def test_strip_survives_case_and_if_blocks_around_embedded_inserts():
    # A CASE *expression*'s END and an END IF must not prematurely drop block depth to 0 -- if they
    # did, the INSERTs after them would be wrongly stripped. The trailing top-level INSERT still is.
    source = (
        "CREATE OR REPLACE PROCEDURE p IS\n"
        "  v NUMBER;\n"
        "BEGIN\n"
        "  v := CASE WHEN 1 = 1 THEN 1 ELSE 2 END;\n"
        "  IF v > 0 THEN\n"
        "    INSERT INTO t VALUES (v);\n"
        "  END IF;\n"
        "  INSERT INTO t2 VALUES (v);\n"
        "END;\n"
        "/\n"
        "INSERT INTO seed VALUES (9);\n"
    )
    result = parse_declarations(source, "p.sql")
    assert [r.table_refs for r in result.routines] == [[("T", "INSERT"), ("T2", "INSERT")]]


# -- Leading documentation comments (doc) -------------------------------------------


def test_doc_captures_a_line_comment_block_above_a_procedure():
    source = (
        "-- Greets a person by name.\n"
        "-- Also logs the greeting to the audit table.\n"
        "CREATE OR REPLACE PROCEDURE greet(p_name IN VARCHAR2) IS\n"
        "BEGIN\n"
        "  log_greeting(p_name);\n"
        "END;\n"
    )
    (routine,) = parse_declarations(source, "greet.prc").routines
    assert routine.doc == "Greets a person by name.\nAlso logs the greeting to the audit table."


def test_doc_captures_a_block_comment_above_a_function():
    source = (
        "/* Doubles the input.\n"
        "   Pure arithmetic, no I/O. */\n"
        "CREATE OR REPLACE FUNCTION double_it(p_n IN NUMBER) RETURN NUMBER IS\n"
        "BEGIN\n"
        "  RETURN p_n * 2;\n"
        "END;\n"
    )
    (routine,) = parse_declarations(source, "double_it.fnc").routines
    assert routine.doc == "Doubles the input.\nPure arithmetic, no I/O."


def test_doc_is_empty_when_a_blank_line_separates_the_comment():
    # A blank line between the comment and the CREATE means it is NOT a doc comment.
    source = (
        "-- This comment is detached from what follows.\n"
        "\n"
        "CREATE OR REPLACE PROCEDURE lonely IS BEGIN NULL; END;\n"
    )
    (routine,) = parse_declarations(source, "lonely.prc").routines
    assert routine.doc == ""


def test_doc_is_empty_when_there_is_no_comment_at_all():
    source = "CREATE OR REPLACE PROCEDURE bare IS BEGIN NULL; END;\n"
    (routine,) = parse_declarations(source, "bare.prc").routines
    assert routine.doc == ""


def test_doc_captures_a_comment_above_a_trigger():
    source = (
        "-- Stamps the row's creation time.\n"
        "CREATE OR REPLACE TRIGGER trg_emp\n"
        "BEFORE INSERT ON employees\n"
        "FOR EACH ROW\n"
        "BEGIN\n"
        "  :NEW.created := SYSDATE;\n"
        "END;\n"
    )
    (trigger,) = parse_declarations(source, "trg_emp.trg").triggers
    assert trigger.doc == "Stamps the row's creation time."


def test_doc_captures_comments_on_a_package_and_its_body_members():
    source = (
        "-- Human resources routines.\n"
        "CREATE OR REPLACE PACKAGE BODY hr IS\n"
        "  -- Hires a new employee.\n"
        "  PROCEDURE hire(p_name IN VARCHAR2) IS\n"
        "  BEGIN INSERT INTO employees(name) VALUES (p_name); END;\n"
        "  /* Returns the current headcount. */\n"
        "  FUNCTION headcount RETURN NUMBER IS v NUMBER;\n"
        "  BEGIN SELECT COUNT(*) INTO v FROM employees; RETURN v; END;\n"
        "END hr;\n"
    )
    (pkg,) = parse_declarations(source, "hr.pkb").packages
    assert pkg.doc == "Human resources routines."
    members = {m.name: m.doc for m in pkg.members}
    assert members == {"HIRE": "Hires a new employee.", "HEADCOUNT": "Returns the current headcount."}


def test_doc_reaches_node_properties_through_resolution():
    # Resolution-level: the captured doc must survive onto the emitted graph node's properties.
    from auryn.core.graph_model import GraphNode
    from auryn.ingestors.plsql import resolution

    source = (
        "-- Records an audit entry.\n"
        "CREATE OR REPLACE PROCEDURE audit_it(p IN VARCHAR2) IS\n"
        "BEGIN INSERT INTO audit_log(msg) VALUES (p); END;\n"
    )
    raw = parse_declarations(source, "audit_it.prc")
    nodes = [n for n in resolution.resolve([raw], "plsql://P") if isinstance(n, GraphNode)]
    proc = next(n for n in nodes if n.label == "Procedure" and n.properties["name"] == "AUDIT_IT")
    assert proc.properties["doc"] == "Records an audit entry."
