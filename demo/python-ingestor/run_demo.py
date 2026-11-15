"""Demo of the Python ingestor: ingest a small multi-module package and show the deep,
in-process semantics the Jedi engine gives that a plain `ast` parser cannot.

Runs the *real* pipeline -- `ingest_directory` over sample/app/ (the default `jedi`
engine) -- then narrates what landed in the graph:

  * class inheritance resolved cross-file, with an ordered MRO;
  * calls split three ways -- import-resolved edge / inferred receiver-typed edge /
    external-dropped;
  * variable/attribute/param types inferred, each carrying its provenance.

Everything here installs with `uv sync` (Jedi is a plain Python dependency) -- no Node, no
build step, no vendored toolchain.

Usage:
    uv run python demo/python-auryn/run_demo.py

Docker is only needed if you don't already have FalkorDB on localhost:6379.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from auryn.core import ingest
from auryn.core.graph_client import FalkorDBConfig, GraphWriter, connect_client
from auryn.core.logging_config import setup_logging

FALKORDB_CONTAINER = "auryn-demo-falkordb"
FALKORDB_PORT = 6379
VIEWER_URL = "http://127.0.0.1:8080"
GRAPH_NAME = "python_demo"
PROJECT = "demo"
_SAMPLE = Path(__file__).resolve().parent / "sample"


def _port_open(port: int, host: str = "localhost", timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _ensure_falkordb() -> None:
    if _port_open(FALKORDB_PORT):
        return
    print("Starting FalkorDB via Docker...")
    subprocess.run(["docker", "rm", "-f", FALKORDB_CONTAINER], capture_output=True)
    subprocess.run(
        ["docker", "run", "-d", "--name", FALKORDB_CONTAINER, "-p", f"{FALKORDB_PORT}:6379", "falkordb/falkordb:latest"],
        check=True,
        capture_output=True,
    )
    for _ in range(60):
        if _port_open(FALKORDB_PORT):
            return
        time.sleep(0.5)
    sys.exit("FalkorDB did not become reachable on localhost:6379")


def _rows(graph, cypher):
    return graph.query(cypher).result_set


def _narrate(graph) -> None:
    print("\n" + "=" * 78)
    print("PYTHON SUBGRAPH (Jedi engine, in-process) -- sample/app/")
    print("=" * 78)

    print("\n1. Inheritance resolved cross-file, with an ordered MRO:")
    for name, res, mro in _rows(
        graph, "MATCH (w:Class {name:'Widget'})-[e:EXTENDS]->(b:Class) RETURN b.name, e.resolution, w.mro"
    ):
        print(f"   Widget --EXTENDS ({res})--> {name}   (in a different module than app.widget)")
        print(f"   MRO: {' -> '.join(mro)}")

    print("\n2. Calls split three ways -- what a plain ast parser can't do:")
    for target, res, recv in _rows(
        graph,
        "MATCH (:Method {name:'render'})-[c:CALLS]->(t) "
        "RETURN t.name, c.resolution, c.receiver_type ORDER BY c.resolution",
    ):
        recv_s = f" on receiver {recv}" if recv else ""
        print(f"   render() --CALLS ({res})--> {target}(){recv_s}")
    dropped = _rows(graph, "MATCH (n) WHERE n.id CONTAINS 'dumps' RETURN count(n)")[0][0]
    print(f"   render() external call json.dumps(...) -> dropped (nodes referencing it: {dropped})")

    print("\n3. Types inferred in-process, each with provenance (annotated | inferred | unresolved):")
    for name, typ, src in _rows(
        graph, "MATCH (a:Attribute) RETURN a.name, a.type, a.type_source ORDER BY a.name"
    ):
        print(f"   attribute {name:6} : {typ:15} [{src}]")
    for label in ("Parameter",):
        rows = _rows(graph, f"MATCH (n:{label}) RETURN n.type_source, count(*) ORDER BY n.type_source")
        print(f"   {label} provenance: {', '.join(f'{s}={c}' for s, c in rows)}")

    print("\n4. Web-framework golden path (route decorator -> handler -> call closure):")
    for method, route, handler in _rows(
        graph, "MATCH (h)-[:EXPOSES_ENDPOINT]->(e:APIEndpoint) RETURN e.http_method, e.route, h.name ORDER BY e.route"
    ):
        print(f"   {method:4} {route:22} --EXPOSES_ENDPOINT<-- {handler}()")
    from auryn.core import graph_queries as gq

    for (ep,) in _rows(graph, "MATCH (e:APIEndpoint {http_method:'POST'}) RETURN e.id"):
        bi = gq.trace_entrypoint(graph, ep)
        if bi:
            closure = ", ".join(sorted(f["name"] for f in bi["functions"]))
            print(f"   trace_entrypoint(POST /widgets) reaches: {closure}")


def main() -> None:
    setup_logging()
    _ensure_falkordb()

    client = connect_client(FalkorDBConfig(host="localhost", port=FALKORDB_PORT))
    if GRAPH_NAME in client.list_graphs():
        client.select_graph(GRAPH_NAME).delete()
    graph = client.select_graph(GRAPH_NAME)

    print(f"Ingesting {_SAMPLE} with the multi-language endpoint (Jedi engine)...")
    report = ingest.ingest_directory(GraphWriter(graph), str(_SAMPLE), project_name=PROJECT)
    for r in report.languages:
        if r.result is not None:
            print(f"  {r.language}: +{r.result.nodes_added} nodes / +{r.result.edges_added} edges from {r.files_detected} files")

    _narrate(graph)

    print(f"\nOpening the viewer at {VIEWER_URL} (graph '{GRAPH_NAME}')...")
    from auryn.viewer.app import create_app

    app = create_app(client)
    threading.Timer(1.0, lambda: webbrowser.open(VIEWER_URL)).start()
    app.run(host="127.0.0.1", port=8080)


if __name__ == "__main__":
    main()
