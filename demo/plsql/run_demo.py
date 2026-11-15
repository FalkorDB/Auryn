"""One-command PL/SQL demo.

Makes sure Oracle and FalkorDB are reachable (starting them via Docker if nothing's
already listening on their default ports), (re)builds the HR demo schema, ingests
it with both ingestors, and opens the graph viewer in your browser.

Usage:
    uv run python demo/plsql/run_demo.py

Docker is only needed if you don't already have Oracle/FalkorDB running locally --
if something's already listening on localhost:1521/6379, this uses it as-is rather
than starting containers of its own.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser

from setup import build_schema

from auryn.core.graph_client import FalkorDBConfig, GraphWriter, connect, connect_client
from auryn.core.logging_config import setup_logging
from auryn.ingestors.plsql.config import PLSQLSourceConfig
from auryn.ingestors.plsql.ingestor import PLSQLIngestor
from auryn.ingestors.sql.config import SQLSourceConfig
from auryn.ingestors.sql.ingestor import SQLIngestor
from auryn.viewer.app import create_app

ORACLE_CONTAINER = "auryn-demo-oracle"
ORACLE_ADMIN_PASSWORD = "welcome123"
ORACLE_PORT = 1521
ORACLE_SERVICE = "XEPDB1"

FALKORDB_CONTAINER = "auryn-demo-falkordb"
FALKORDB_PORT = 6379

VIEWER_URL = "http://127.0.0.1:8080"
DATABASE_NAME = "hrdemo"


def _port_open(port: int, host: str = "localhost", timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _container_exists(name: str) -> bool:
    result = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return name in result.stdout.split()


def _wait_for_log(name: str, pattern: str, timeout: float = 240) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
        if pattern in result.stdout or pattern in result.stderr:
            return
        time.sleep(2)
    raise TimeoutError(f"Timed out waiting for {name!r} to log {pattern!r}")


def _ensure_container(name: str, port: int, run_args: list[str], ready_log_pattern: str) -> None:
    """Reuses an existing container by this name if there is one, else uses whatever
    is already listening on `port` if there is one, else creates a fresh container.
    Only touches Docker at all when there's actually a reason to."""
    docker_available = shutil.which("docker") is not None

    if docker_available and _container_exists(name):
        print(f"Starting existing '{name}' container...")
        subprocess.run(["docker", "start", name], check=True, capture_output=True)
        print(f"Waiting for {name} to be ready...")
        _wait_for_log(name, ready_log_pattern)
        return

    if _port_open(port):
        print(f"Something's already listening on :{port} -- using it as-is.")
        return

    if not docker_available:
        sys.exit(
            f"Nothing is listening on :{port} and Docker isn't on PATH. Either "
            f"install Docker, or start the service yourself and re-run this script."
        )

    print(f"Creating '{name}' container...")
    subprocess.run(["docker", "run", "-d", "--name", name, *run_args], check=True, capture_output=True)
    print(f"Waiting for {name} to be ready...")
    _wait_for_log(name, ready_log_pattern)


def main() -> None:
    _ensure_container(
        FALKORDB_CONTAINER,
        FALKORDB_PORT,
        ["-p", f"{FALKORDB_PORT}:6379", "falkordb/falkordb:latest"],
        "Ready to accept connections",
    )
    _ensure_container(
        ORACLE_CONTAINER,
        ORACLE_PORT,
        [
            "-p",
            f"{ORACLE_PORT}:1521",
            "-e",
            f"ORACLE_PASSWORD={ORACLE_ADMIN_PASSWORD}",
            "gvenzl/oracle-xe:21-slim-faststart",
        ],
        "DATABASE IS READY TO USE!",
    )

    print()
    admin_dsn = f"system/{ORACLE_ADMIN_PASSWORD}@localhost:{ORACLE_PORT}/{ORACLE_SERVICE}"
    dsns = build_schema(admin_dsn)

    setup_logging()
    falkordb_config = FalkorDBConfig(host="localhost", port=FALKORDB_PORT)
    writer = GraphWriter(connect(falkordb_config))

    print()
    print("Ingesting SQL schema...")
    sql_stats = SQLIngestor(SQLSourceConfig(dsn=dsns["sqlalchemy_dsn"], database_name=DATABASE_NAME)).run(
        writer
    )
    print(f"  {sql_stats.nodes_merged} nodes, {sql_stats.edges_merged} edges")

    print("Ingesting PL/SQL...")
    plsql_stats = PLSQLIngestor(
        PLSQLSourceConfig(dsn=dsns["schema_dsn"], database_name=DATABASE_NAME)
    ).run(writer)
    print(f"  {plsql_stats.nodes_merged} nodes, {plsql_stats.edges_merged} edges")

    app = create_app(connect_client(falkordb_config))
    print()
    print(f"Opening the graph viewer at {VIEWER_URL} (Ctrl-C here stops the viewer)...")
    threading.Timer(1.5, lambda: webbrowser.open(VIEWER_URL)).start()
    app.run(host="127.0.0.1", port=8080)


if __name__ == "__main__":
    main()
