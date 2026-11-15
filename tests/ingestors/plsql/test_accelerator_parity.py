"""Guards the optional C++ accelerator (tools/plsql-accel -> `plsql_accel`) against drift from the
pure-Python extraction. Skipped entirely when the accelerator wheel isn't installed. When it is, the
whole point is that parse_declarations must return a *byte-identical* RawPlsqlFile whether it went
through C++ or Python -- so this parses a spread of constructs both ways and asserts equality.

If this fails, the C++ extractor (src/plsql_accel.cpp) and the Python visitors
(source_analysis._DeclarationVisitor / _CallsAndTableRefsVisitor) have diverged -- fix whichever
changed, or rebuild the accelerator against the current grammar.
"""

from __future__ import annotations

import dataclasses

import pytest

import auryn.ingestors.plsql.source_analysis as sa

pytestmark = pytest.mark.skipif(sa._accel is None, reason="plsql_accel accelerator wheel not installed")

_SAMPLES = {
    "package_body": """CREATE OR REPLACE PACKAGE BODY hr IS
      PROCEDURE hire(p_name IN VARCHAR2, p_dept IN NUMBER DEFAULT 1) IS
      BEGIN INSERT INTO employees(name, dept_id) VALUES (p_name, p_dept); audit_log('hire'); END;
      FUNCTION headcount(p_dept IN NUMBER) RETURN NUMBER IS v NUMBER;
      BEGIN SELECT COUNT(*) INTO v FROM employees WHERE dept_id = p_dept; RETURN v; END;
    END hr;""",
    "package_spec": """CREATE OR REPLACE PACKAGE hr IS
      PROCEDURE hire(p_name IN VARCHAR2);
      FUNCTION headcount RETURN NUMBER;
    END hr;""",
    "standalone_proc": """CREATE OR REPLACE PROCEDURE audit_it(p IN VARCHAR2) IS
    BEGIN INSERT INTO audit_log(msg) VALUES (p); END;""",
    "trigger": """CREATE OR REPLACE TRIGGER emp_bi BEFORE INSERT ON employees FOR EACH ROW
    BEGIN :new.created := SYSDATE; log_change('emp'); END;""",
    "type": "CREATE OR REPLACE TYPE addr_t AS OBJECT (street VARCHAR2(80), city VARCHAR2(40));",
    "table": """CREATE TABLE employees (
      id NUMBER(10) NOT NULL,
      name VARCHAR2(100) DEFAULT 'x' NOT NULL,
      dept_id NUMBER,
      salary NUMBER DEFAULT 0,
      CONSTRAINT pk_emp PRIMARY KEY (id),
      CONSTRAINT fk_dept FOREIGN KEY (dept_id) REFERENCES departments (id),
      CONSTRAINT uq_name UNIQUE (name),
      CONSTRAINT ck_sal CHECK (salary >= 0));""",
    "table_inline": """CREATE TABLE t (
      id NUMBER PRIMARY KEY,
      dept NUMBER REFERENCES depts(id),
      code VARCHAR2(10) NOT NULL UNIQUE,
      amt NUMBER CHECK (amt > 0));""",
    "alter": """ALTER TABLE employees ADD (bonus NUMBER DEFAULT 0);
    ALTER TABLE employees ADD CONSTRAINT fk_mgr FOREIGN KEY (mgr_id) REFERENCES employees(id);""",
    "view": "CREATE OR REPLACE FORCE VIEW active_emps AS SELECT e.id, e.name FROM employees e JOIN departments d ON d.id = e.dept_id WHERE e.salary > 0;",
    "index": "CREATE UNIQUE INDEX ix_emp_name ON employees(name, dept_id);",
    "sequence": "CREATE SEQUENCE emp_seq START WITH 1 INCREMENT BY 1;",
    "multi": """CREATE TABLE a (id NUMBER PRIMARY KEY);
    CREATE OR REPLACE PROCEDURE p IS BEGIN UPDATE a SET id = 1; DELETE FROM a; SELECT id FROM a; END;
    CREATE INDEX ix ON a(id);""",
}


@pytest.mark.parametrize("name", list(_SAMPLES))
def test_accelerator_matches_python_extraction(name, monkeypatch):
    source = _SAMPLES[name]
    accel_result = sa.parse_declarations(source, f"{name}.sql")
    monkeypatch.setattr(sa, "_accel", None)
    python_result = sa.parse_declarations(source, f"{name}.sql")
    assert dataclasses.asdict(accel_result) == dataclasses.asdict(python_result)
