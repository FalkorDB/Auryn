#!/usr/bin/env python
"""Dockerfile ingestor + deployment-tier demo (ADR 0007).

Ingests a tiny two-service system -- an `api` and a `worker`, each a Python package with its own
Dockerfile -- into ONE graph, then shows the deployment tier the `auryn-docker` plugin builds and the
`deploy` enrichment stitches onto the code:

    * BuildStage nodes (one per `FROM ... AS`), with their base images and multi-stage COPY --from;
    * the shared `Image` hub -- both services' `python:3.12-slim` base collapses to ONE node;
    * PACKAGES -- the code seam: which Python modules each build stage actually ships;
    * the headline cross-tier query -- from a function's module to the image that ships it.

Run:  uv run python demo/docker-deploy/run_demo.py
(Needs a FalkorDB: a local one on :6379, or the embedded extra `auryn-core[embedded]`.)
"""

from __future__ import annotations

import os
import signal
import socket
import tempfile
from pathlib import Path

from auryn.core.graph_client import GraphWriter
from auryn.core.ingest import ingest_directory

SAMPLE = Path(__file__).resolve().parent / "sample"
GRAPH = "demo_docker_deploy"
BAR = "─" * 78


def make_client():
    """A local FalkorDB on :6379 if present, else the in-process embedded build. Returns
    (client, backend_label, embedded_pid) -- the pid is captured so the demo can reap the embedded
    server on exit instead of leaking it."""
    try:
        with socket.create_connection(("localhost", 6379), timeout=0.5):
            pass
        from falkordb import FalkorDB

        return FalkorDB(host="localhost", port=6379), "localhost:6379", None
    except OSError:
        pass
    try:
        from redislite.falkordb_client import FalkorDB as Embedded

        conn = Embedded(str(Path(tempfile.mkdtemp()) / "demo.db"))
        pid = getattr(getattr(conn, "connection", None), "pid", None)
        return conn, "embedded falkordblite", pid
    except ImportError as e:
        raise SystemExit(
            "No FalkorDB available. Start one on localhost:6379 (or `auryn quickstart`), "
            "or install the embedded extra: pip install 'auryn-core[embedded]'."
        ) from e


def _shutdown(conn, pid) -> None:
    """Reap ONLY the embedded server this demo started -- identified by the pid captured at connect.
    A shared localhost FalkorDB (pid is None) belongs to the user, so we must never send it Redis
    SHUTDOWN or signal it -- just leave it running. For our own embedded server: graceful SHUTDOWN
    first, then SIGTERM the tracked pid, so no redis-server/falkordblite process is left behind."""
    if not (isinstance(pid, int) and pid > 0):
        return  # shared / user-owned server -- not ours to stop
    for obj in (getattr(conn, "connection", None), conn):
        fn = getattr(obj, "shutdown", None)
        if callable(fn):
            try:
                fn()
            except Exception:
                pass  # Redis SHUTDOWN closes the socket before replying -- the raised error is expected
            break
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass  # already gone (the graceful SHUTDOWN above reaped it) -- nothing left to signal


def rows(g, cypher, params=None):
    return g.ro_query(cypher, params or {}).result_set


def section(title: str) -> None:
    print(f"\n{BAR}\n{title}\n{BAR}")


def main() -> None:
    conn, backend, pid = make_client()
    print(f"\nFalkorDB backend: {backend}")
    try:
        try:
            conn.select_graph(GRAPH).delete()  # idempotent: start from a clean graph each run
        except Exception:
            pass  # no such graph yet (first run) -- nothing to delete
        g = conn.select_graph(GRAPH)
        ingest_directory(GraphWriter(g), str(SAMPLE), project_name="shop", deep="off", auto_install=False)

        section("1. BUILD STAGES  (one per `FROM ... AS`, with its base)")
        for path, stage, base in rows(g,
            "MATCH (f:File)-[:HAS_STAGE]->(s:BuildStage) RETURN f.path, s.stage, s.base_ref "
            "ORDER BY f.path, s.index"):
            print(f"  {path:28}  stage {stage:8}  FROM {base}")

        section("2. SHARED IMAGE HUB  (one node per normalized ref; N dependents)")
        for ref, deps, resolved in rows(g,
            "MATCH (i:Image) OPTIONAL MATCH (:BuildStage)-[:BASED_ON]->(i) "
            "WITH i, count(*) AS deps RETURN i.ref, deps, i.resolved ORDER BY i.ref"):
            tag = "built-in-graph" if resolved else "external pull"
            print(f"  {ref:34}  <- {deps} stage(s)   [{tag}]")

        section("3. PACKAGES  (the code seam: which modules each stage ships)")
        for stage, mod, path in rows(g,
            "MATCH (s:BuildStage)-[:PACKAGES]->(m) RETURN s.id, m.name, coalesce(m.path, m.file_path) "
            "ORDER BY s.id"):
            print(f"  stage {_short(stage):20}  ships  {mod}  ({path})")

        section("4. MULTI-STAGE COPY --from  (stage -> stage)")
        for a, b in rows(g, "MATCH (a:BuildStage)-[:COPIES_FROM]->(b:BuildStage) RETURN a.stage, b.stage"):
            print(f"  {a}  COPIES_FROM  {b}")

        section("5. CROSS-TIER  (a function -> its module -> the image that ships it, + its base)")
        for fn, mod, stage, base in rows(g,
            "MATCH (fn:Function)<-[:HAS_FUNCTION|HAS_METHOD*0..2]-(m:Module)<-[:PACKAGES]-(s:BuildStage) "
            "OPTIONAL MATCH (s)-[:BASED_ON]->(i:Image) "
            "RETURN fn.name, m.name, s.stage, coalesce(i.ref, '(stage)') ORDER BY fn.name"):
            print(f"  {fn}()  in {mod}  ->  shipped by stage '{stage}'  (base {base})")

        print(f"\n{BAR}\nDone. Inspect it yourself:  uv run auryn view   (graph: {GRAPH})\n{BAR}\n")
    finally:
        _shutdown(conn, pid)


def _short(node_id: str) -> str:
    return node_id.split("/file/", 1)[-1] if "/file/" in node_id else node_id


if __name__ == "__main__":
    main()
