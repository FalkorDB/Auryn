"""Agent usage statistics for the MCP server.

Every MCP tool call IS an agent accessing the code graph, and the tool name IS the operation.
`core/mcp/server.py` wraps each tool so a completed call appends one JSONL event here; `ingestor
stats` aggregates them. The point is visibility into *how well an agent uses the code graph* --
how often it reaches for the graph, which operations, how many land vs error, and whether queries
come back with anything.

Design: append-only JSONL under the home dir (durable, survives sessions), one line per call
(O_APPEND keeps concurrent MCP servers from interleaving). Recording is entirely best-effort --
a telemetry failure must never disturb a tool call. Local-only; nothing is transmitted. Opt out
with `AURYN_USAGE_STATS=0`.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from auryn.core.home import usage_log_path

# One id per MCP-server process ~= one agent session (Claude Code spawns one server per session).
_SESSION = f"{os.getpid()}-{uuid.uuid4().hex[:6]}"

# Result keys whose list length gauges "did this call return anything" (across the query tools).
_LIST_KEYS = ("results", "nodes", "edges", "callers", "callees", "reachable", "projects")


def enabled() -> bool:
    return os.environ.get("AURYN_USAGE_STATS", "1").strip().lower() not in ("0", "off", "false", "no")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _result_size(result: object) -> int | None:
    """Best-effort count of what a tool returned (rows/nodes/edges), or None if not countable --
    a cheap signal for whether the agent's queries are productive."""
    if not isinstance(result, dict):
        return None
    total, found = 0, False
    for k in _LIST_KEYS:
        v = result.get(k)
        if isinstance(v, list):
            total += len(v)
            found = True
    if found:
        return total
    nc = result.get("node_count")
    return nc if isinstance(nc, int) else None


def record_call(*, graph: str, tool: str, op: str, status: str, duration_ms: float,
                result: object = None, error: str | None = None) -> None:
    """Append one usage event. Swallows every error -- telemetry never breaks a tool call."""
    if not enabled():
        return
    try:
        event: dict[str, object] = {
            "ts": _now_iso(), "session": _SESSION, "graph": graph,
            "tool": tool, "op": op, "status": status, "duration_ms": round(duration_ms, 1),
        }
        size = _result_size(result)
        if size is not None:
            event["result_size"] = size
        if error:
            event["error"] = error[:160]
        path = usage_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:  # O_APPEND: atomic for one short line
            f.write(json.dumps(event, separators=(",", ":")) + "\n")
    except Exception:
        pass


# -- reading / aggregation (for `auryn stats`) -----------------------------------------

def parse_since(spec: str) -> str:
    """A relative window (`30m`, `24h`, `7d`, `90s`) -> an ISO cutoff; anything else is treated as
    an ISO date/timestamp prefix and compared as-is (isoformat sorts lexicographically)."""
    m = re.fullmatch(r"(\d+)([smhd])", spec.strip())
    if not m:
        return spec.strip()
    secs = int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
    return (datetime.now(timezone.utc) - timedelta(seconds=secs)).isoformat(timespec="seconds")


def read_events(*, since: str | None = None, graph: str | None = None,
                path: Path | None = None) -> list[dict]:
    path = path or usage_log_path()
    if not path.exists():
        return []
    cutoff = parse_since(since) if since else None
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if graph and e.get("graph") != graph:
            continue
        if cutoff and str(e.get("ts", "")) < cutoff:
            continue
        out.append(e)
    return out


def summarize(events: list[dict]) -> dict:
    """Headline totals + a per-tool breakdown (count, read/write, errors, avg latency, avg result
    size) -- the shape `auryn stats` prints."""
    by_op: Counter = Counter()
    sessions: set = set()
    graphs: set = set()
    times: list[str] = []
    errors = 0
    by_tool: dict[str, dict] = defaultdict(
        lambda: {"count": 0, "op": "?", "errors": 0, "ms": 0.0, "result": 0, "result_n": 0})
    for e in events:
        tool = e.get("tool", "?")
        d = by_tool[tool]
        d["count"] += 1
        d["op"] = e.get("op", d["op"])
        d["ms"] += float(e.get("duration_ms", 0) or 0)
        if e.get("status") == "error":
            d["errors"] += 1
            errors += 1
        if isinstance(e.get("result_size"), int):
            d["result"] += e["result_size"]
            d["result_n"] += 1
        by_op[e.get("op", "?")] += 1
        sessions.add(e.get("session"))
        if e.get("graph"):
            graphs.add(e["graph"])
        if e.get("ts"):
            times.append(e["ts"])
    return {
        "total": len(events),
        "errors": errors,
        "sessions": len({s for s in sessions if s}),
        "graphs": sorted(graphs),
        "first": min(times) if times else None,
        "last": max(times) if times else None,
        "by_op": dict(by_op),
        "by_tool": {t: dict(v) for t, v in by_tool.items()},
    }


def group_counts(events: list[dict], key: str) -> dict[str, int]:
    """Access counts grouped by an event field (`session`, `graph`, `op`, `tool`)."""
    c: Counter = Counter(str(e.get(key)) for e in events)
    return dict(c.most_common())
