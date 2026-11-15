"""Builds the PL/SQL demo schema against a running Oracle instance.

Creates a dedicated user ("hr_demo") with an HR-style schema (regions, countries,
locations, departments, employees, job_history, audit_log), a sequence, a view, an
explicit index, PK/UNIQUE/CHECK constraints, a package with an overloaded procedure,
a standalone procedure that calls into the package, and two triggers -- enough to
exercise every entity type both the SQL and PL/SQL ingestors model.

Usage:
    python demo/plsql/setup.py --admin-dsn "system/yourpassword@localhost:1521/XEPDB1"

Safe to re-run: drops and recreates the hr_demo user first.
"""

from __future__ import annotations

import argparse

import oracledb

SCHEMA = "hr_demo"
PASSWORD = "hr_demo_pass123"

DDL = [
    "CREATE TABLE regions (region_id NUMBER PRIMARY KEY, region_name VARCHAR2(50) NOT NULL)",
    """CREATE TABLE countries (
        country_id CHAR(2) PRIMARY KEY,
        country_name VARCHAR2(50) NOT NULL,
        region_id NUMBER NOT NULL REFERENCES regions(region_id)
    )""",
    """CREATE TABLE locations (
        location_id NUMBER PRIMARY KEY,
        city VARCHAR2(50) NOT NULL,
        country_id CHAR(2) NOT NULL REFERENCES countries(country_id)
    )""",
    """CREATE TABLE jobs (
        job_id VARCHAR2(10) PRIMARY KEY,
        job_title VARCHAR2(50) NOT NULL,
        min_salary NUMBER,
        max_salary NUMBER
    )""",
    """CREATE TABLE departments (
        department_id NUMBER PRIMARY KEY,
        department_name VARCHAR2(50) NOT NULL,
        location_id NUMBER REFERENCES locations(location_id),
        manager_id NUMBER
    )""",
    """CREATE TABLE employees (
        employee_id NUMBER PRIMARY KEY,
        first_name VARCHAR2(50),
        last_name VARCHAR2(50) NOT NULL,
        email VARCHAR2(100) NOT NULL,
        hire_date DATE NOT NULL,
        job_id VARCHAR2(10) NOT NULL REFERENCES jobs(job_id),
        salary NUMBER NOT NULL,
        department_id NUMBER REFERENCES departments(department_id),
        manager_id NUMBER REFERENCES employees(employee_id),
        CONSTRAINT uq_employees_email UNIQUE (email),
        CONSTRAINT ck_employees_salary CHECK (salary > 0)
    )""",
    # departments.manager_id -> employees is circular with employees.department_id ->
    # departments, so it's added after both tables exist rather than inline above.
    "ALTER TABLE departments ADD CONSTRAINT fk_departments_manager "
    "FOREIGN KEY (manager_id) REFERENCES employees(employee_id)",
    """CREATE TABLE job_history (
        employee_id NUMBER NOT NULL REFERENCES employees(employee_id),
        start_date DATE NOT NULL,
        end_date DATE,
        job_id VARCHAR2(10) REFERENCES jobs(job_id),
        department_id NUMBER REFERENCES departments(department_id),
        CONSTRAINT pk_job_history PRIMARY KEY (employee_id, start_date)
    )""",
    """CREATE TABLE audit_log (
        log_id NUMBER PRIMARY KEY,
        message VARCHAR2(200) NOT NULL,
        logged_at DATE DEFAULT SYSDATE NOT NULL
    )""",
    "CREATE INDEX ix_employees_last_name ON employees(last_name)",
    "CREATE SEQUENCE employees_seq START WITH 200 INCREMENT BY 1",
    "CREATE SEQUENCE audit_log_seq START WITH 1 INCREMENT BY 1",
    """CREATE OR REPLACE VIEW employee_directory AS
        SELECT e.employee_id, e.first_name, e.last_name, e.email,
               d.department_name, j.job_title
        FROM employees e
        JOIN departments d ON e.department_id = d.department_id
        JOIN jobs j ON e.job_id = j.job_id""",
]

SAMPLE_DATA = [
    "INSERT INTO regions VALUES (1, 'Europe')",
    "INSERT INTO regions VALUES (2, 'Americas')",
    "INSERT INTO countries VALUES ('UK', 'United Kingdom', 1)",
    "INSERT INTO countries VALUES ('US', 'United States', 2)",
    "INSERT INTO locations VALUES (1000, 'London', 'UK')",
    "INSERT INTO locations VALUES (1400, 'South San Francisco', 'US')",
    "INSERT INTO jobs VALUES ('IT_PROG', 'Programmer', 4000, 10000)",
    "INSERT INTO jobs VALUES ('SA_REP', 'Sales Representative', 3000, 8000)",
    "INSERT INTO jobs VALUES ('HR_REP', 'HR Representative', 3500, 7000)",
    "INSERT INTO departments (department_id, department_name, location_id) VALUES (10, 'Engineering', 1400)",
    "INSERT INTO departments (department_id, department_name, location_id) VALUES (20, 'Sales', 1000)",
    "INSERT INTO departments (department_id, department_name, location_id) VALUES (30, 'Human Resources', 1000)",
    """INSERT INTO employees (employee_id, first_name, last_name, email, hire_date, job_id, salary, department_id, manager_id)
       VALUES (100, 'Alice', 'Chen', 'alice.chen@example.com', DATE '2018-01-05', 'IT_PROG', 9500, 10, NULL)""",
    """INSERT INTO employees (employee_id, first_name, last_name, email, hire_date, job_id, salary, department_id, manager_id)
       VALUES (101, 'Ben', 'Okafor', 'ben.okafor@example.com', DATE '2019-03-11', 'IT_PROG', 6200, 10, 100)""",
    """INSERT INTO employees (employee_id, first_name, last_name, email, hire_date, job_id, salary, department_id, manager_id)
       VALUES (102, 'Carla', 'Diaz', 'carla.diaz@example.com', DATE '2020-07-22', 'SA_REP', 5400, 20, NULL)""",
    """INSERT INTO employees (employee_id, first_name, last_name, email, hire_date, job_id, salary, department_id, manager_id)
       VALUES (103, 'Dev', 'Kumar', 'dev.kumar@example.com', DATE '2021-02-14', 'SA_REP', 4800, 20, 102)""",
    """INSERT INTO employees (employee_id, first_name, last_name, email, hire_date, job_id, salary, department_id, manager_id)
       VALUES (104, 'Elin', 'Svensson', 'elin.svensson@example.com', DATE '2017-11-30', 'HR_REP', 5600, 30, NULL)""",
    "UPDATE departments SET manager_id = 100 WHERE department_id = 10",
    "UPDATE departments SET manager_id = 102 WHERE department_id = 20",
    "UPDATE departments SET manager_id = 104 WHERE department_id = 30",
    """INSERT INTO job_history (employee_id, start_date, end_date, job_id, department_id)
       VALUES (101, DATE '2019-03-11', DATE '2020-12-31', 'IT_PROG', 10)""",
]

PLSQL = [
    """CREATE OR REPLACE PACKAGE hr_pkg IS
        PROCEDURE hire_employee(
            p_first_name VARCHAR2, p_last_name VARCHAR2, p_email VARCHAR2,
            p_job_id VARCHAR2, p_salary NUMBER, p_department_id NUMBER
        );
        PROCEDURE give_raise(p_employee_id NUMBER, p_percent NUMBER);
        FUNCTION get_department_headcount(p_department_id NUMBER) RETURN NUMBER;
        FUNCTION get_average_salary(p_department_id NUMBER) RETURN NUMBER;
        PROCEDURE log_change(p_employee_id NUMBER);
        PROCEDURE log_change(p_message VARCHAR2);
    END hr_pkg;""",
    """CREATE OR REPLACE PACKAGE BODY hr_pkg IS
        PROCEDURE hire_employee(
            p_first_name VARCHAR2, p_last_name VARCHAR2, p_email VARCHAR2,
            p_job_id VARCHAR2, p_salary NUMBER, p_department_id NUMBER
        ) IS
        BEGIN
            INSERT INTO employees (employee_id, first_name, last_name, email, hire_date, job_id, salary, department_id)
            VALUES (employees_seq.NEXTVAL, p_first_name, p_last_name, p_email, SYSDATE, p_job_id, p_salary, p_department_id);
            log_change('Hired ' || p_first_name || ' ' || p_last_name);
        END hire_employee;

        PROCEDURE give_raise(p_employee_id NUMBER, p_percent NUMBER) IS
        BEGIN
            UPDATE employees SET salary = salary * (1 + p_percent / 100) WHERE employee_id = p_employee_id;
            log_change('Gave a ' || p_percent || '% raise to employee ' || p_employee_id);
        END give_raise;

        FUNCTION get_department_headcount(p_department_id NUMBER) RETURN NUMBER IS
            v_count NUMBER;
        BEGIN
            SELECT COUNT(*) INTO v_count FROM employees WHERE department_id = p_department_id;
            RETURN v_count;
        END get_department_headcount;

        FUNCTION get_average_salary(p_department_id NUMBER) RETURN NUMBER IS
            v_avg NUMBER;
        BEGIN
            SELECT AVG(salary) INTO v_avg FROM employees WHERE department_id = p_department_id;
            RETURN v_avg;
        END get_average_salary;

        PROCEDURE log_change(p_employee_id NUMBER) IS
        BEGIN
            INSERT INTO job_history (employee_id, start_date, end_date, job_id, department_id)
            SELECT employee_id, SYSDATE, NULL, job_id, department_id FROM employees WHERE employee_id = p_employee_id;
        END log_change;

        PROCEDURE log_change(p_message VARCHAR2) IS
        BEGIN
            INSERT INTO audit_log (log_id, message) VALUES (audit_log_seq.NEXTVAL, p_message);
        END log_change;
    END hr_pkg;""",
    """CREATE OR REPLACE PROCEDURE transfer_employee(p_employee_id NUMBER, p_new_department_id NUMBER) IS
    BEGIN
        UPDATE employees SET department_id = p_new_department_id WHERE employee_id = p_employee_id;
        hr_pkg.log_change(p_employee_id);
    END transfer_employee;""",
    """CREATE OR REPLACE TRIGGER trg_employees_salary_check
        BEFORE INSERT OR UPDATE OF salary ON employees
        FOR EACH ROW
    DECLARE
        v_min_salary NUMBER;
        v_max_salary NUMBER;
    BEGIN
        SELECT min_salary, max_salary INTO v_min_salary, v_max_salary FROM jobs WHERE job_id = :NEW.job_id;
        IF :NEW.salary < v_min_salary OR :NEW.salary > v_max_salary THEN
            RAISE_APPLICATION_ERROR(-20001, 'Salary out of range for this job');
        END IF;
    END;""",
    """CREATE OR REPLACE TRIGGER trg_employees_audit_insert
        AFTER INSERT ON employees
        FOR EACH ROW
    BEGIN
        INSERT INTO audit_log (log_id, message)
        VALUES (audit_log_seq.NEXTVAL, 'New employee inserted: ' || :NEW.employee_id);
    END;""",
]


def build_schema(admin_dsn: str) -> dict[str, str]:
    """(re)builds the hr_demo user/schema against a reachable Oracle instance.
    Safe to call repeatedly -- drops and recreates the user first. Returns the
    connection strings ingestion needs: {"schema_dsn": ..., "sqlalchemy_dsn": ...}."""
    admin_conn = oracledb.connect(dsn=admin_dsn)
    admin_cur = admin_conn.cursor()

    print(f"Dropping user {SCHEMA} if it exists...")
    try:
        admin_cur.execute(f"DROP USER {SCHEMA} CASCADE")
    except oracledb.DatabaseError:
        pass  # didn't exist yet

    print(f"Creating user {SCHEMA}...")
    admin_cur.execute(f"CREATE USER {SCHEMA} IDENTIFIED BY {PASSWORD}")
    admin_cur.execute(
        f"GRANT CONNECT, RESOURCE, CREATE VIEW, CREATE SEQUENCE, CREATE TRIGGER, "
        f"CREATE PROCEDURE TO {SCHEMA}"
    )
    admin_cur.execute(f"ALTER USER {SCHEMA} QUOTA UNLIMITED ON USERS")
    admin_conn.commit()
    admin_conn.close()

    # Reuse the admin DSN's host/port/service, just swap credentials. Parsed into
    # parts (not just string-substituted) so the SQLAlchemy URL below can use
    # "?service_name=" -- SQLAlchemy's oracle+oracledb dialect needs that query-param
    # form, not "host:port/service_name" as a path segment.
    _, location = admin_dsn.split("@", 1)
    host_port, service_name = location.rsplit("/", 1)
    schema_dsn = f"{SCHEMA}/{PASSWORD}@{host_port}/{service_name}"
    conn = oracledb.connect(dsn=schema_dsn)
    cur = conn.cursor()

    print("Creating tables/sequences/view/index...")
    for stmt in DDL:
        cur.execute(stmt)

    print("Loading sample data...")
    for stmt in SAMPLE_DATA:
        cur.execute(stmt)

    print("Creating package/procedure/triggers...")
    for stmt in PLSQL:
        cur.execute(stmt)

    conn.commit()
    conn.close()

    sqlalchemy_dsn = f"oracle+oracledb://{SCHEMA}:{PASSWORD}@{host_port}/?service_name={service_name}"
    return {"schema_dsn": schema_dsn, "sqlalchemy_dsn": sqlalchemy_dsn}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--admin-dsn",
        required=True,
        help='Admin (e.g. system) connect string, e.g. "system/yourpassword@localhost:1521/XEPDB1".',
    )
    args = parser.parse_args()

    dsns = build_schema(args.admin_dsn)

    print()
    print("Done. Ingest with:")
    print()
    print(f'  uv run auryn run sql --dsn "{dsns["sqlalchemy_dsn"]}" --database-name hrdemo')
    print(f'  uv run auryn run oracle --dsn "{dsns["schema_dsn"]}" --database-name hrdemo')


if __name__ == "__main__":
    main()
