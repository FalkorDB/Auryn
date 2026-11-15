"""Shared fixtures: a FalkorDB connection and a disposable graph per test.

The connection defaults to the serverless **embedded** backend (falkordblite; no local
server or Docker needed) -- set AURYN_TEST_BACKEND=server for a real server instead
(local-first on :6379, else a Docker testcontainer). See _test_backend().

Ingestor-level tests (tests/ingestors/<name>/) are what actually exercise these --
there's no standalone "does FalkorDB connect" test.
"""

from __future__ import annotations

import os
import socket
from uuid import uuid4

import pytest
from falkordb import FalkorDB

from auryn.core.graph_client import SHARED_NODE_LABEL as _SHARED_NODE_LABEL


@pytest.fixture(autouse=True)
def _no_auto_install(monkeypatch):
    """Auto-install of missing ingestor plugins is opt-OUT (on by default), so a fixture
    containing a catalogued file type (.c/.go/.ts/...) would otherwise trigger a real PyPI
    install. Force it off for every test; tests that exercise the install path opt back in
    explicitly."""
    monkeypatch.setenv("AURYN_AUTO_INSTALL", "0")


@pytest.fixture(autouse=True)
def _isolate_cache_home(tmp_path, monkeypatch):
    """Point the on-disk caches -- the git clone cache (`repos/`) and the per-language parse caches --
    at a per-test directory instead of the shared `~/.cache/auryn`. A test that doesn't pass an
    explicit `cache_root` (e.g. the python git_sync tests) would otherwise write the user's home cache,
    which under the parallel default (`-n auto`) means several workers writing the same parse-cache
    pickles non-atomically. `git_source.cache_home()` reads `$AURYN_CACHE_HOME` at call time, so this
    holds no matter which module imported the cache root or when."""
    monkeypatch.setenv("AURYN_CACHE_HOME", str(tmp_path / "auryn_cache"))


# Container/SDK-backed fixtures are session/module-scoped and EXPENSIVE -- a database testcontainer
# (Oracle/PostgreSQL/MySQL) or the published Roslyn extractor. Under the parallel default each xdist
# worker instantiates session fixtures independently, so the same resource spread across workers spins
# up DUPLICATE containers. Pin every test needing a given resource to one worker (an `xdist_group` +
# `--dist loadgroup`, see pyproject addopts): exactly one container per resource, while DIFFERENT
# resources -- and the ~1000 light tests -- still spread across workers. (CI isolates these in a
# separate `-n0` integration job; this keeps a LOCAL `pytest` run from duplicating them too.)
_RESOURCE_GROUP = {
    "oracle_container": "oracle", "oracle_schema": "oracle",
    "postgres_dsn": "postgres", "mysql_dsn": "mysql", "roslyn_tool_env": "roslyn",
}


@pytest.hookimpl(tryfirst=True)  # MUST precede xdist's own modifyitems, which reads the xdist_group
def pytest_collection_modifyitems(config, items):  # mark and bakes it into the nodeid (@group) at collection
    for item in items:
        fixtures = set(getattr(item, "fixturenames", ()))
        for fixname, group in _RESOURCE_GROUP.items():
            if fixname in fixtures:
                item.add_marker(pytest.mark.xdist_group(group))
                break


def _test_backend() -> str:
    """Which FalkorDB the suite runs against. Default is `embedded` -- the serverless,
    in-process falkordblite build -- so the suite needs neither a local FalkorDB nor
    Docker. Set AURYN_TEST_BACKEND=server to run against a real server instead
    (local-first on :6379, else a Docker testcontainer). This only swaps the backend the
    `falkordb_connection` fixture yields; because everything (ingestors, viewer, MCP) obtains
    its connection from that one fixture, both backends exercise identical code."""
    return os.environ.get("AURYN_TEST_BACKEND", "embedded").strip().lower()


def _local_falkordb_available(host: str = "localhost", port: int = 6379) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _shutdown_embedded(conn) -> None:
    """Best-effort stop of the child redis-server falkordblite manages, so a session doesn't
    leak a process. redislite also registers an atexit handler, so any failure here is
    harmless (SHUTDOWN legitimately drops the connection before replying)."""
    for obj in (getattr(conn, "connection", None), conn):
        shutdown = getattr(obj, "shutdown", None)
        if callable(shutdown):
            try:
                shutdown()
                return
            except Exception:
                pass


@pytest.fixture(scope="session")
def falkordb_connection(tmp_path_factory):
    if _test_backend() == "embedded":
        try:
            from redislite.falkordb_client import FalkorDB as EmbeddedFalkorDB
        except ImportError as e:  # embedded is an optional extra
            raise RuntimeError(
                "Embedded test backend needs the 'embedded' extra. Install it "
                "(`pip install 'auryn-core[embedded]'`, already in the dev group) or run "
                "against a server with AURYN_TEST_BACKEND=server."
            ) from e
        db_path = tmp_path_factory.mktemp("falkordb_embedded") / "graph.db"
        conn = EmbeddedFalkorDB(str(db_path))
        try:
            yield conn
        finally:
            _shutdown_embedded(conn)
        return

    if _local_falkordb_available():
        yield FalkorDB(host="localhost", port=6379)
        return

    from testcontainers.core.container import DockerContainer
    from testcontainers.core.wait_strategies import LogMessageWaitStrategy

    container = DockerContainer("falkordb/falkordb:latest").with_exposed_ports(6379)
    container.waiting_for(LogMessageWaitStrategy(r"Ready to accept connections"))
    with container:
        host = container.get_container_host_ip()
        port = int(container.get_exposed_port(6379))
        yield FalkorDB(host=host, port=port)


class _EntityStampingGraph:
    """Test-only wrapper around a FalkorDB Graph. In production EVERY node carries the system-wide
    :Entity label (added by GraphWriter at write time) so a by-id lookup that can't name the label
    -- the MCP neighbors/callers/callees/path/get_node tools -- is an index seek, not an all-node
    scan (see graph_client.SHARED_NODE_LABEL). Tests that hand-build a graph with raw `CREATE`
    bypass GraphWriter and would lack the label, so those queries would find nothing. This wrapper
    stamps :Entity on every id'd node right after a CREATE, so a hand-seeded test graph matches what
    production actually stores. MERGE writes (GraphWriter's own path) already carry the label, so the
    stamp is scoped to CREATE to avoid re-scanning after every batched write."""

    def __init__(self, g, name=None):
        # Two call forms: wrap an existing Graph (`_EntityStampingGraph(graph)`), or mirror the real
        # FalkorDB Graph constructor (`_EntityStampingGraph(client, name)`) so `type(graph)(client,
        # name)` -- the idiom a test uses to make a sibling graph on the same client -- still yields a
        # stamping wrapper.
        if name is not None:
            from falkordb import Graph

            g = Graph(g, name)
        self._g = g

    def __getattr__(self, name):
        return getattr(self._g, name)

    def query(self, q, params=None, **kwargs):
        result = self._g.query(q, params, **kwargs)
        if "CREATE" in q.upper():
            self._g.query(f"MATCH (n) WHERE n.id IS NOT NULL SET n:{_SHARED_NODE_LABEL}")
        return result


@pytest.fixture
def graph(falkordb_connection):
    from redis.exceptions import ResponseError

    g = _EntityStampingGraph(falkordb_connection.select_graph(f"ingestor_test_{uuid4().hex}"))
    yield g
    try:
        g.delete()
    except ResponseError:
        # A test that legitimately wrote nothing (e.g. ingesting a repo with no
        # supported files, or an operation that errored before its first write)
        # never brought the graph key into existence -- FalkorDB then rejects
        # delete with "Invalid graph operation on empty key". Nothing to clean up.
        pass


@pytest.fixture(scope="session")
def oracle_container():
    """Unlike FalkorDB, nobody has Oracle "already running" for unrelated reasons --
    no local-first check, always a fresh testcontainer. Session-scoped because Oracle
    takes real time to initialize; sharing one instance across the whole PL/SQL test
    session keeps the suite from taking many minutes."""
    from testcontainers.community.oracle import OracleDbContainer

    # Deliberately omits dbname: OracleDbContainer would set ORACLE_DATABASE to it,
    # which collides with "XEPDB1" -- the gvenzl/oracle-xe image already creates a
    # pluggable database with that exact name by default (confirmed against a live
    # container: passing dbname="XEPDB1" here fails with
    # "ORA-65012: Pluggable database XEPDB1 already exists"). oracle_schema below
    # builds connection strings by hand rather than via get_connection_url(), which
    # only knows the oracle-free image family's defaults ("FREE"/"FREEPDB1").
    with OracleDbContainer(
        image="gvenzl/oracle-xe:21-slim-faststart",
        oracle_password="testpass123",
    ) as container:
        yield container


@pytest.fixture(scope="session")
def oracle_schema(oracle_container):
    """Creates one dedicated Oracle user (not SYSTEM, which is full of built-in
    objects that make assertions noisy) with a small sample schema -- a table, two
    procedures (one calling the other), a function, a package, and a trigger, plus a
    `wrap`-obfuscated function and package (secret_double / secret_pkg) --
    covering every entity the PL/SQL ingestor models. Session-scoped: Oracle DDL is
    slow enough that per-test schemas would make the suite crawl; individual tests
    read this same schema and each get their own fresh FalkorDB graph instead.

    Returns a dict with `sqlalchemy_dsn` (for SQLIngestor) and `oracledb_dsn` (for
    PLSQLIngestor), plus the schema/user name.
    """
    import oracledb

    host = oracle_container.get_container_host_ip()
    port = int(oracle_container.get_exposed_port(1521))
    admin_dsn = f"system/testpass123@{host}:{port}/XEPDB1"

    schema = "test_plsql"
    password = "testpass123"

    admin_conn = oracledb.connect(dsn=admin_dsn)
    admin_cur = admin_conn.cursor()
    admin_cur.execute(f"CREATE USER {schema} IDENTIFIED BY {password}")
    admin_cur.execute(
        f"GRANT CONNECT, RESOURCE, CREATE VIEW, CREATE SEQUENCE, CREATE TRIGGER, "
        f"CREATE PROCEDURE TO {schema}"
    )
    admin_cur.execute(f"ALTER USER {schema} QUOTA UNLIMITED ON USERS")
    admin_conn.commit()
    admin_conn.close()

    schema_dsn = f"{schema}/{password}@{host}:{port}/XEPDB1"
    conn = oracledb.connect(dsn=schema_dsn)
    cur = conn.cursor()
    for stmt in [
        "CREATE TABLE employees (id NUMBER PRIMARY KEY, name VARCHAR2(100), last_greeted DATE)",
        """CREATE OR REPLACE PROCEDURE log_greeting(p_name IN VARCHAR2) IS
           BEGIN
             UPDATE employees SET last_greeted = SYSDATE WHERE name = p_name;
           END;""",
        """CREATE OR REPLACE PROCEDURE greet(p_name IN VARCHAR2) IS
           BEGIN
             log_greeting(p_name);
           END;""",
        """CREATE OR REPLACE FUNCTION greet_count(p_name IN VARCHAR2) RETURN NUMBER IS
             v_count NUMBER;
           BEGIN
             SELECT COUNT(*) INTO v_count FROM employees WHERE name = p_name;
             RETURN v_count;
           END;""",
        """CREATE OR REPLACE PACKAGE greetings_pkg IS
             PROCEDURE greet_all;
             FUNCTION double_it(p_n IN NUMBER) RETURN NUMBER;
             PROCEDURE notify(p_name IN VARCHAR2);
             PROCEDURE notify(p_id IN NUMBER);
           END greetings_pkg;""",
        """CREATE OR REPLACE PACKAGE BODY greetings_pkg IS
             PROCEDURE greet_all IS
             BEGIN
               greet('all');
             END greet_all;
             FUNCTION double_it(p_n IN NUMBER) RETURN NUMBER IS
             BEGIN
               RETURN p_n * 2;
             END double_it;
             PROCEDURE notify(p_name IN VARCHAR2) IS
             BEGIN
               NULL;
             END notify;
             PROCEDURE notify(p_id IN NUMBER) IS
             BEGIN
               NULL;
             END notify;
           END greetings_pkg;""",
        """CREATE OR REPLACE TRIGGER trg_employees_biu
           BEFORE INSERT OR UPDATE ON employees
           FOR EACH ROW
           BEGIN
             :NEW.last_greeted := SYSDATE;
           END;""",
        # `wrap`-obfuscated objects (as vendor-shipped PL/SQL arrives): DBMS_DDL.CREATE_WRAPPED
        # compiles genuinely wrapped units, so ALL_SOURCE stores bytecode whose first line ends
        # in the `wrapped` keyword. The dictionary still exposes their signatures/args -- so the
        # ingestor must still create the nodes, just flag them wrapped with a null source.
        "BEGIN DBMS_DDL.CREATE_WRAPPED('CREATE OR REPLACE FUNCTION secret_double(p IN NUMBER) "
        "RETURN NUMBER IS BEGIN RETURN p * 2; END;'); END;",
        "BEGIN DBMS_DDL.CREATE_WRAPPED('CREATE OR REPLACE PACKAGE secret_pkg IS "
        "PROCEDURE do_secret(p_id IN NUMBER); END secret_pkg;'); END;",
        "BEGIN DBMS_DDL.CREATE_WRAPPED('CREATE OR REPLACE PACKAGE BODY secret_pkg IS "
        "PROCEDURE do_secret(p_id IN NUMBER) IS BEGIN NULL; END; END secret_pkg;'); END;",
    ]:
        cur.execute(stmt)
    conn.commit()
    conn.close()

    return {
        "schema": schema,
        "sqlalchemy_dsn": f"oracle+oracledb://{schema}:{password}@{host}:{port}/?service_name=XEPDB1",
        "oracledb_dsn": schema_dsn,
    }
