"""Fixtures for the generic SQL ingestor's live-engine tests. SQLite needs nothing (stdlib,
see test_sql_ingestor.py); Postgres and MySQL each spin up a Docker testcontainer, shared
across the module's tests (containers are slow to start). Every test that needs one is skipped
cleanly when Docker isn't available, so the suite still runs on a machine without it."""

from __future__ import annotations

import socket

import pytest


def _docker_available() -> bool:
    try:
        import docker  # testcontainers' backend
    except ImportError:
        return False
    try:
        docker.from_env().ping()
        return True
    except Exception:
        return False


def _wait_port(host: str, port: int, timeout: float = 30.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return
        except OSError:
            time.sleep(0.5)
    raise TimeoutError(f"{host}:{port} not reachable within {timeout}s")


@pytest.fixture(scope="module")
def postgres_dsn():
    """A SQLAlchemy DSN for a throwaway PostgreSQL testcontainer (psycopg2 driver)."""
    if not _docker_available():
        pytest.skip("Docker not available -- skipping live PostgreSQL test")
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine", driver="psycopg2") as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="module")
def mysql_dsn():
    """A SQLAlchemy DSN for a throwaway MySQL testcontainer (pymysql driver)."""
    if not _docker_available():
        pytest.skip("Docker not available -- skipping live MySQL test")
    from testcontainers.community.mysql import MySqlContainer

    with MySqlContainer("mysql:8.0", dialect="pymysql") as my:
        yield my.get_connection_url()
