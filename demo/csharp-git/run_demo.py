"""One-command git-sync demo.

Makes sure FalkorDB is reachable (starting it via Docker if nothing's already
listening on its default port), builds the sample git repository (three tagged
commits showing a DTO reclassification flip forward and back -- see setup.py),
syncs the graph through all three while narrating what changed at each step, then
opens the graph viewer -- from which you can keep moving between commits yourself
via the "Git-tracked projects" panel (the tags show up right in its branch/tag
dropdown, no need to paste a commit sha).

Usage:
    uv run python demo/csharp-git/run_demo.py

Docker is only needed if you don't already have FalkorDB running locally -- if
something's already listening on localhost:6379, this uses it as-is. Unlike the
plain C# demo, there's no working tree to point at either -- git_sync reads
commits directly out of the repository's object database.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser

import git
from setup import build_repo

from auryn.core.graph_client import FalkorDBConfig, GraphWriter, connect, connect_client
from auryn.core.logging_config import setup_logging
from auryn.ingestors.csharp.git_sync import GitCSharpProjectConfig, sync
from auryn.viewer.app import create_app

FALKORDB_CONTAINER = "auryn-demo-falkordb"
FALKORDB_PORT = 6379

VIEWER_URL = "http://127.0.0.1:8080"
PROJECT_NAME = "Widgets"
SOLUTION_NAME = "Acme"


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


def _describe_widget(graph) -> str:
    row = graph.query("MATCH (n {name: 'Widget'}) RETURN labels(n)[0]").result_set
    label = row[0][0] if row else "(not yet ingested)"
    return f"Widget is currently labeled: {label}"


def main() -> None:
    _ensure_falkordb()

    print()
    repo_path = build_repo()
    print(f"Built sample repo at {repo_path}")

    setup_logging()
    falkordb_config = FalkorDBConfig(host="localhost", port=FALKORDB_PORT)
    graph = connect(falkordb_config)
    writer = GraphWriter(graph)

    config = GitCSharpProjectConfig(repo_uri=str(repo_path), project_name=PROJECT_NAME, solution_name=SOLUTION_NAME)

    repo = git.Repo(str(repo_path))
    commits = list(reversed(list(repo.iter_commits("main"))))
    tag_by_commit = {t.commit.hexsha: t.name for t in repo.tags}
    for i, commit in enumerate(commits, start=1):
        tag = tag_by_commit[commit.hexsha]
        print(f"\nSyncing to commit {i}/3 ({tag}: {commit.message.strip()})...")
        stats = sync(writer, config, ref=tag)
        print(
            f"  nodes +{stats.nodes_added}/-{stats.nodes_removed}/~{stats.nodes_updated}, "
            f"edges +{stats.edges_added}/-{stats.edges_removed}/~{stats.edges_updated} "
            f"({stats.elapsed_seconds:.2f}s)"
        )
        print(f"  {_describe_widget(graph)}")

    print("\nTry moving between commits yourself:")
    for i, commit in enumerate(commits, start=1):
        tag = tag_by_commit[commit.hexsha]
        # --host/--port: the viewer above reads the Docker FalkorDB; without them `git-sync`
        # writes to the configured backend, and the open viewer would never change.
        print(f'  uv run auryn git-sync --host localhost --port {FALKORDB_PORT} csharp --repo-uri "{repo_path}" '
              f'--project-name {PROJECT_NAME} --solution-name {SOLUTION_NAME} --ref {tag}  # commit {i}')
    print(
        '...or open the viewer\'s "Git-tracked projects" panel and pick '
        "v1-core-domain / v2-http-api / v3-dedicated-dtos from the Widgets project's dropdown."
    )

    app = create_app(connect_client(falkordb_config))
    print()
    print(f"Opening the graph viewer at {VIEWER_URL} (Ctrl-C here stops the viewer)...")
    threading.Timer(1.5, lambda: webbrowser.open(VIEWER_URL)).start()
    app.run(host="127.0.0.1", port=8080)


if __name__ == "__main__":
    main()
