"""One-command "real, large C# codebase" demo.

Runs `auryn git-sync` against dotnet/aspnetcore -- the same public repository
(and the same two adjacent release tags) `benchmarks/test_csharp_ingest_benchmark.py`
uses as its baseline, see benchmarks/README.md for why this one was picked -- to
show git_sync holding up at real scale: tens of thousands of files, hundreds of
thousands of nodes/edges, not the small hand-authored samples the other C#
demos use.

Two syncs against one (dedicated, `GRAPH_NAME`) graph:
  1. cold ingest at `COLD_REF` into an empty graph -- every file gets parsed for
     the first time.
  2. incremental resync to `WARM_REF`, a newer adjacent real tag -- only the
     handful of files that actually changed between the two releases get
     re-parsed; everything else is a parse-cache hit, even though the repo as a
     whole is huge.

Usage:
    uv run python demo/csharp-large/run_demo.py

Docker is only needed if you don't already have FalkorDB running locally -- if
something's already listening on localhost:6379, this uses it as-is. Expect the
first run to take several minutes (cloning ~377MB and parsing several thousand
.cs files); the git clone and parse cache this leaves behind under
~/.cache/auryn are shared with `benchmarks/` (and vice versa) -- whichever you
run first warms the cache for the other.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser

from auryn.core.graph_client import FalkorDBConfig, GraphWriter, connect, connect_client
from auryn.core.logging_config import setup_logging
from auryn.ingestors.csharp.git_sync import GitCSharpProjectConfig, SyncStats, sync
from auryn.viewer.app import create_app

FALKORDB_CONTAINER = "auryn-demo-falkordb"
FALKORDB_PORT = 6379

VIEWER_URL = "http://127.0.0.1:8080"

REPO_URI = "https://github.com/dotnet/aspnetcore.git"
PROJECT_NAME = "AspNetCore"
COLD_REF = "v9.0.17"
WARM_REF = "v9.0.18"
GRAPH_NAME = "csharp_large_demo"


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


def _report(title: str, ref: str, stats: SyncStats) -> None:
    print(f"\n{title} ({ref})...")
    print(
        f"  nodes +{stats.nodes_added}/-{stats.nodes_removed}/~{stats.nodes_updated}, "
        f"edges +{stats.edges_added}/-{stats.edges_removed}/~{stats.edges_updated} "
        f"({stats.elapsed_seconds:.1f}s)"
    )
    print(f"  {stats.files_parsed} files parsed, {stats.files_cached} files reused from the parse cache")


def main() -> None:
    _ensure_falkordb()

    setup_logging()
    falkordb_config = FalkorDBConfig(host="localhost", port=FALKORDB_PORT, graph_name=GRAPH_NAME)
    writer = GraphWriter(connect(falkordb_config))
    config = GitCSharpProjectConfig(repo_uri=REPO_URI, project_name=PROJECT_NAME)

    print(f"\nSyncing {PROJECT_NAME} from {REPO_URI} into the '{GRAPH_NAME}' graph...")
    print(
        "First run clones ~377MB and parses several thousand .cs files -- this can take "
        "several minutes. (Already run `benchmarks/`? The git clone and parse cache under "
        "~/.cache/auryn are shared, so this'll be much quicker.)"
    )

    stats = sync(writer, config, ref=COLD_REF)
    _report("Cold ingest", COLD_REF, stats)

    stats = sync(writer, config, ref=WARM_REF)
    _report("Incremental resync (adjacent real release)", WARM_REF, stats)

    print("\nRe-run this script any time -- syncing to a ref the graph is already at is a no-op.")

    app = create_app(connect_client(falkordb_config))
    print()
    print(f"Opening the graph viewer at {VIEWER_URL} (Ctrl-C here stops the viewer)...")
    threading.Timer(1.5, lambda: webbrowser.open(VIEWER_URL)).start()
    app.run(host="127.0.0.1", port=8080)


if __name__ == "__main__":
    main()
