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

        section("5. SERVICES  (compose: what each runs + what it depends on)")
        for svc, img in rows(g, "MATCH (s:Service)-[:RUNS_IMAGE]->(i:Image) RETURN s.name, i.ref ORDER BY s.name"):
            print(f"  service {svc:8}  RUNS  {img}")
        for a, b in rows(g, "MATCH (a:Service)-[:DEPENDS_ON]->(b:Service) RETURN a.name, b.name ORDER BY a.name, b.name"):
            print(f"  service {a:8}  DEPENDS_ON  {b}")

        section("6. THE BUILD->RUN LOOP  (compose closes it; images flip internal)")
        for ref, resolved in rows(g, "MATCH (i:Image) RETURN i.ref, i.resolved ORDER BY i.resolved DESC, i.ref"):
            kind = "built-in-graph (internal)" if resolved else "external pull"
            print(f"  {ref:34}  [{kind}]")
        print()
        for stage, img, svc in rows(g,
            "MATCH (st:BuildStage)-[:BUILDS_IMAGE]->(i:Image)<-[:RUNS_IMAGE]-(s:Service) "
            "RETURN st.id, i.ref, s.name ORDER BY s.name"):
            print(f"  stage {_short(stage):20}  BUILDS  {img}  <-  run by service '{svc}'")

        section("7. FULL-STACK BLAST RADIUS  (a function -> ... -> the running service)")
        for fn, img, svc in rows(g,
            "MATCH (fn:Function)<-[:HAS_FUNCTION]-(:Module)<-[:PACKAGES]-(:BuildStage)"
            "-[:BUILDS_IMAGE]->(i:Image)<-[:RUNS_IMAGE]-(s:Service) "
            "RETURN fn.name, i.ref, s.name ORDER BY fn.name"):
            print(f"  change {fn}()  ->  ships {img}  ->  run by service '{svc}'")

        section("8. CLUSTER  (k8s: workloads run the SAME hub image; Ingress exposes an external route)")
        for kind, name, img in rows(g,
            "MATCH (s:Service)-[:RUNS_IMAGE]->(i:Image) WHERE s.kind IS NOT NULL "
            "RETURN s.kind, s.name, i.ref ORDER BY s.name"):
            print(f"  {kind} {name:6}  RUNS  {img}")
        for host, route, svc in rows(g,
            "MATCH (s:Service)-[:SERVES]->(e:APIEndpoint) OPTIONAL MATCH (ing:Ingress {route:e.route}) "
            "RETURN ing.host, e.route, s.name ORDER BY e.route"):
            print(f"  https://{host}{route}  -SERVES->  APIEndpoint  (by workload '{svc}')")

        section("9. CLOUD  (terraform: resources, the dependency graph, and the SAME hub image)")
        for rtype, name, img in rows(g,
            "MATCH (r:Resource)-[:RUNS_IMAGE]->(i:Image) RETURN r.rtype, r.name, i.ref ORDER BY r.name"):
            print(f"  {rtype} {name:6}  RUNS  {img}")
        for a, b in rows(g, "MATCH (a:Resource)-[:DEPENDS_ON]->(b:Resource) RETURN a.address, b.address"):
            print(f"  {a}  DEPENDS_ON  {b}")
        for rtype, name, kind in rows(g,
            "MATCH (r:Resource)-[:PROVISIONS]->(n) "
            "RETURN r.rtype, r.name, head([l IN labels(n) WHERE l <> 'Entity']) ORDER BY r.name"):
            print(f"  {rtype} {name:6}  PROVISIONS  a {kind}")

        # How many producers reach the shared api image (1 builder + N runners) -- reported from the
        # graph, not assumed.
        prod = rows(g, "MATCH (n)-[e:BUILDS_IMAGE|RUNS_IMAGE]->(:Image {ref:'docker.io/shop/api:1.0'}) "
                       "RETURN count(DISTINCT n)")
        n = prod[0][0] if prod else 0
        print(f"\n{BAR}\n{n} producers converge on one hub image (shop/api:1.0).  "
              f"Inspect it:  uv run auryn view   (graph: {GRAPH})\n{BAR}\n")
    finally:
        _shutdown(conn, pid)


def _short(node_id: str) -> str:
    return node_id.split("/file/", 1)[-1] if "/file/" in node_id else node_id


if __name__ == "__main__":
    main()
