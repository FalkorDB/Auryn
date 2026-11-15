#!/usr/bin/env python
"""Agent interaction, live and deterministic.

Drives the SAME MCP tools Claude Code uses, over the SAME in-process protocol the tests use, and
prints the answers an agent would get. Run it after each ingest step to watch the answers change:
on a single repo the cross-repo calls are honest placeholders; once the dependency is ingested,
the very same questions resolve end-to-end. In real use Claude Code calls these tools itself.

Usage:  uv run python demo/presentation/ask_agent.py [graph_name]     (default: acme)
"""

from __future__ import annotations

import asyncio
import sys

from mcp.client import Client

from auryn.cli import _resolve_connection
from auryn.core.graph_client import connect_client
from auryn.mcp.server import create_server

GRAPH = sys.argv[1] if len(sys.argv) > 1 else "acme"
BAR = "─" * 80


def _server():
    return create_server(connect_client(_resolve_connection(None, None, None, None, False)), GRAPH)


async def _call(server, tool, args):
    async with Client(server) as c:
        return (await c.call_tool(tool, args)).structured_content


def qname(node):
    return node.get("properties", {}).get("qualified_name") or node.get("name")


def project(node_id):
    s = str(node_id)
    return "external" if s.startswith("extern://") else s.split("://", 1)[-1].split("/", 1)[0]


def state(node):
    p = node.get("properties", {})
    if p.get("resolved") is False:
        return f"unresolved · {p.get('origin')}"
    return "resolved"


async def main() -> None:
    server = _server()
    print(f"\n{BAR}\n AGENT ↔ code graph   ·   graph: {GRAPH}\n{BAR}")

    print("\nQ1. auryn_status()  — what's in the graph?")
    st = await _call(server, "auryn_status", {})
    print("    " + ", ".join(f"{d['label']}:{d['count']}" for d in st.get("labels", [])))
    projs = ", ".join(p.get("name", "?") for p in st.get("projects", [])) or "—"
    print(f"    repos: {projs}")

    print("\nQ2. trace_entrypoint('POST /checkout')  — what does this endpoint do, end to end?")
    ep = await _call(server, "locate", {"name": "checkout", "kind": "APIEndpoint"})
    if ep["results"]:
        tr = await _call(server, "trace_entrypoint", {"entry_id": ep["results"][0]["id"]})
        print(f"    {tr['entry']['name']}  ↴")
        for f in tr.get("functions", []):
            print(f"      → {qname(f):34} [{project(f['id']):13}] {state(f)}")
        if not tr.get("functions"):
            print("      (reaches nothing yet)")

    print("\nQ3. who_calls('charge')  — impact / blast radius across every repo")
    loc = await _call(server, "locate", {"name": "charge"})
    real = next((r for r in loc["results"] if not str(r["id"]).startswith("extern://")), None)
    if real:
        wc = await _call(server, "who_calls", {"node_id": real["id"]})
        for n in wc.get("nodes", []):
            print(f"      • {project(n['id']):18} {qname(n)}")
    else:
        print("      charge is only a placeholder here — its defining repo isn't in the graph yet.")

    print("\nQ4. audit — every platform / builtin call the whole system makes")
    audit = await _call(server, "run_cypher", {
        "query": "MATCH (c)-[:CALLS]->(x:Function) WHERE x.resolved = false RETURN DISTINCT x"})
    for n in sorted(audit.get("nodes", []), key=lambda n: (n.get("properties", {}).get("origin", ""), qname(n) or "")):
        origin = n.get("properties", {}).get("origin")
        flag = "   ⚠ review" if origin == "builtin" and "eval" in str(qname(n)) else ""
        print(f"      • {qname(n):26} [{origin}]{flag}")

    print(f"\n{BAR}\n Each answer above is one graph query — precise, and it spans every repo in the graph.\n{BAR}\n")


if __name__ == "__main__":
    asyncio.run(main())
