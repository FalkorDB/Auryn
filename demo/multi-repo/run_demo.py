#!/usr/bin/env python
"""Multi-repo code graph demo (ADR 0004).

Three repositories that reference each other:

    billing/    a shared library     -> defines billing.core.charge / refund
    orders/     a web service        -> imports billing, calls charge; uses os.getenv, eval
    reporting/  a service            -> imports billing, calls charge/refund; uses subprocess

Ingested ONE-AT-A-TIME (a graph per repo) the tools can't see across the boundary: a call into
another repo is an honest but unresolved placeholder. Ingested TOGETHER into ONE graph, the same
calls HEAL into real cross-repo edges and the system becomes queryable as a whole -- impact
analysis, dependency mapping, and a security/portability audit that all span repositories.

Run:  uv run python demo/multi-repo/run_demo.py
(Needs a FalkorDB: a local one on :6379, or the embedded extra `auryn-core[embedded]`.)
"""

from __future__ import annotations

import socket
from pathlib import Path

from auryn.core.graph_client import GraphWriter
from auryn.core.ingest import ingest_directory

REPOS = Path(__file__).resolve().parent / "repos"
BAR = "─" * 78


def make_client():
    """A local FalkorDB on :6379 if present, else the in-process embedded build."""
    try:
        with socket.create_connection(("localhost", 6379), timeout=0.5):
            pass
        from falkordb import FalkorDB
        return FalkorDB(host="localhost", port=6379), "localhost:6379"
    except OSError:
        pass
    try:
        import tempfile
        from redislite.falkordb_client import FalkorDB as Embedded
        return Embedded(str(Path(tempfile.mkdtemp()) / "demo.db")), "embedded falkordblite"
    except ImportError as e:
        raise SystemExit(
            "No FalkorDB available. Start one on localhost:6379 (or `auryn quickstart`), "
            "or install the embedded extra: pip install 'auryn-core[embedded]'."
        ) from e


def reset(client, graph_name: str):
    try:
        client.select_graph(graph_name).delete()  # idempotent: fresh graph each run
    except Exception:
        pass


def ingest(client, graph_name: str, repo: str):
    g = client.select_graph(graph_name)
    ingest_directory(GraphWriter(g), str(REPOS / repo), deep="off", auto_install=False)
    return g


def rows(g, cypher, params=None):
    return g.ro_query(cypher, params or {}).result_set


def project_of(node_id: str) -> str:
    return node_id.split("://", 1)[1].split("/", 1)[0] if "://" in node_id else "?"


def main() -> None:
    client, backend = make_client()
    print(f"\nFalkorDB backend: {backend}\n")

    # ── A. One graph per repo (the default) ──────────────────────────────────────────────
    print(BAR)
    print("A. INGEST EACH REPO ON ITS OWN (a graph per repository)")
    print(BAR)
    reset(client, "demo_orders_solo")
    g_orders = ingest(client, "demo_orders_solo", "orders")
    r = rows(g_orders,
             "MATCH (:Function {name:'place_order'})-[e:CALLS]->(c) WHERE c.name='charge' "
             "RETURN c.id, c.resolved, e.resolution")
    cid, resolved, res = r[0]
    print("  orders.place_order calls charge -> ", cid)
    print(f"    resolved={resolved}   edge.resolution={res!r}")
    print("  ⇒ On its own, the orders graph CANNOT reach billing. `charge` is an honest")
    print("    placeholder (resolved=false); \"who else calls billing.charge?\" is unanswerable")
    print("    because the other repos simply aren't in this graph.\n")

    # ── B. All repos in ONE shared graph ─────────────────────────────────────────────────
    print(BAR)
    print("B. INGEST ALL THREE INTO ONE SHARED GRAPH  (ingest --graph-name system …)")
    print(BAR)
    reset(client, "demo_system")
    g = None
    for repo in ("billing", "orders", "reporting"):
        g = ingest(client, "demo_system", repo)
        print(f"  + ingested {repo}")
    print()

    # Same call as in A -- now healed to billing's real definition.
    r = rows(g, "MATCH (:Function {name:'place_order'})-[e:CALLS]->(c) WHERE c.name='charge' "
                "RETURN c.id, c.resolved, e.resolution")
    cid, resolved, res = r[0]
    print("  orders.place_order calls charge -> ", cid)
    print(f"    resolved={resolved}   edge.resolution={res!r}   ⇒ healed into a real cross-repo edge\n")

    # Q1 — impact / blast radius across repos.
    print("  Q1. \"If billing.charge changes, what breaks — across every repo?\"")
    for caller_id, qn in rows(g,
            "MATCH (caller)-[:CALLS]->(c:Function {qualified_name:'billing.core.charge'}) "
            "WHERE c.resolved IS NULL RETURN caller.id, caller.qualified_name ORDER BY caller.id"):
        print(f"       • {project_of(caller_id):10} {qn}")

    # Q2 — cross-repo dependency mapping.
    print("\n  Q2. \"What does the `orders` service depend on from other repositories?\"")
    for qn, in rows(g,
            "MATCH (f)-[:CALLS]->(t) WHERE f.id STARTS WITH 'python://orders/' "
            "AND t.id STARTS WITH 'python://billing/' RETURN DISTINCT t.qualified_name"):
        print(f"       • orders → billing::{qn}")

    # Q3 — system / security audit across the whole system.
    print("\n  Q3. \"What platform/builtin calls does the WHOLE system make?\" (audit)")
    for qn, origin, callers in rows(g,
            "MATCH (caller)-[:CALLS]->(x:Function) WHERE x.resolved = false "
            "RETURN x.qualified_name, x.origin, collect(DISTINCT caller.qualified_name) "
            "ORDER BY x.origin, x.qualified_name"):
        flag = "  ⚠ review" if origin == "builtin" and "eval" in qn else ""
        print(f"       • {qn:20} [{origin}]  ← {', '.join(sorted(callers))}{flag}")

    print("\n" + BAR)
    print("Same three services, same code. The only difference is whether they share a graph.")
    print("Per-repo: locally correct, blind across boundaries. Shared: the system is one graph")
    print("you can ask impact, dependency, and audit questions of — the multi-repo advantage.")
    print(BAR + "\n")


if __name__ == "__main__":
    main()
