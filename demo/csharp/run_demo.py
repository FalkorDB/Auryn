"""One-command C# demo.

Makes sure FalkorDB is reachable (starting it via Docker if nothing's already
listening on its default port), ingests the sample C# project in this directory,
and opens the graph viewer in your browser.

Usage:
    uv run python demo/csharp/run_demo.py

Docker is only needed if you don't already have FalkorDB running locally -- if
something's already listening on localhost:6379, this uses it as-is rather than
starting a container of its own. Unlike the SQL/PL/SQL demos, there's no external
database to stand up first -- the C# ingestor reads source files directly.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from auryn.core.graph_client import FalkorDBConfig, GraphWriter, connect, connect_client
from auryn.core.logging_config import setup_logging
from auryn.ingestors.csharp.config import CSharpSourceConfig
from auryn.ingestors.csharp.ingestor import CSharpIngestor
from auryn.viewer.app import create_app

FALKORDB_CONTAINER = "auryn-demo-falkordb"
FALKORDB_PORT = 6379

VIEWER_URL = "http://127.0.0.1:8080"
SRC_ROOT = Path(__file__).parent / "src"


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


def _wait_for_log(name: str, pattern: str, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
        if pattern in result.stdout or pattern in result.stderr:
            return
        time.sleep(1)
    raise TimeoutError(f"Timed out waiting for {name!r} to log {pattern!r}")


def _ensure_falkordb() -> None:
    """Reuses an existing container by this name if there is one, else uses
    whatever is already listening on the FalkorDB port if there is one, else
    creates a fresh container. Only touches Docker at all when there's actually a
    reason to."""
    docker_available = shutil.which("docker") is not None

    if docker_available and _container_exists(FALKORDB_CONTAINER):
        print(f"Starting existing '{FALKORDB_CONTAINER}' container...")
        subprocess.run(["docker", "start", FALKORDB_CONTAINER], check=True, capture_output=True)
        print("Waiting for FalkorDB to be ready...")
        _wait_for_log(FALKORDB_CONTAINER, "Ready to accept connections")
        return

    if _port_open(FALKORDB_PORT):
        print(f"Something's already listening on :{FALKORDB_PORT} -- using it as-is.")
        return

    if not docker_available:
        sys.exit(
            f"Nothing is listening on :{FALKORDB_PORT} and Docker isn't on PATH. Either "
            f"install Docker, or start FalkorDB yourself and re-run this script."
        )

    print(f"Creating '{FALKORDB_CONTAINER}' container...")
    subprocess.run(
        ["docker", "run", "-d", "--name", FALKORDB_CONTAINER, "-p", f"{FALKORDB_PORT}:6379", "falkordb/falkordb:latest"],
        check=True,
        capture_output=True,
    )
    print("Waiting for FalkorDB to be ready...")
    _wait_for_log(FALKORDB_CONTAINER, "Ready to accept connections")


def main() -> None:
    _ensure_falkordb()

    setup_logging()
    falkordb_config = FalkorDBConfig(host="localhost", port=FALKORDB_PORT)
    writer = GraphWriter(connect(falkordb_config))

    print()
    print(f"Ingesting C# source from {SRC_ROOT}...")
    config = CSharpSourceConfig(path=str(SRC_ROOT), project_name="Widgets.Api", solution_name="Acme")
    stats = CSharpIngestor(config).run(writer)
    print(f"  {stats.nodes_merged} nodes, {stats.edges_merged} edges")

    app = create_app(connect_client(falkordb_config))
    print()
    print(f"Opening the graph viewer at {VIEWER_URL} (Ctrl-C here stops the viewer)...")
    threading.Timer(1.5, lambda: webbrowser.open(VIEWER_URL)).start()
    app.run(host="127.0.0.1", port=8080)


if __name__ == "__main__":
    main()
