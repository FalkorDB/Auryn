#!/usr/bin/env python
"""Drive the real auryn MCP tools in-process against the `meridian` graph.

Same tools Claude Code calls over MCP, same in-process protocol the tests use -- so the
output captured here is exactly what an agent receives. Used to record the tutorial's
agent<->code graph interactions.

Usage:
    uv run python demo/tutorial-meridian/ask.py <tool> '<json-args>'
    uv run python demo/tutorial-meridian/ask.py locate '{"name": "charge"}'
"""

from __future__ import annotations

import asyncio
import json
import sys

from mcp.client import Client

from auryn.cli import _resolve_connection
from auryn.core.graph_client import connect_client
from auryn.mcp.server import create_server

GRAPH = "meridian"


def _server():
    return create_server(connect_client(_resolve_connection(None, None, None, None, False)), GRAPH)


async def main() -> None:
    tool = sys.argv[1]
    args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    server = _server()
    async with Client(server) as c:
        result = (await c.call_tool(tool, args)).structured_content
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
