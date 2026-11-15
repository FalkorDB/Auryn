"""Agent usage statistics (core/usage.py): recording is best-effort JSONL, aggregation feeds
`auryn stats`. The MCP-level wiring (a tool call records an event) is covered in
tests/mcp/test_server.py."""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def usage(monkeypatch, tmp_path):
    """core.usage pointed at a throwaway home dir, stats enabled."""
    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AURYN_USAGE_STATS", "1")
    import auryn.core.usage as u
    return importlib.reload(u)


def test_record_call_appends_event(usage):
    usage.record_call(graph="g1", tool="locate", op="read", status="ok", duration_ms=12.3,
                      result={"results": [1, 2, 3]})
    events = usage.read_events()
    assert len(events) == 1
    e = events[0]
    assert (e["graph"], e["tool"], e["op"], e["status"]) == ("g1", "locate", "read", "ok")
    assert e["duration_ms"] == 12.3 and e["result_size"] == 3
    assert e["session"] and e["ts"]  # a session id + timestamp are stamped


def test_error_and_result_size_variants(usage):
    usage.record_call(graph="g", tool="get_context", op="read", status="error", duration_ms=1,
                      error="ValueError: no node with id 'x'")
    usage.record_call(graph="g", tool="find_path", op="read", status="ok", duration_ms=2,
                      result={"nodes": [1, 2], "edges": [1, 2, 3]})  # summed
    usage.record_call(graph="g", tool="auryn_status", op="read", status="ok", duration_ms=3,
                      result={"has_data": True})  # no countable list -> no result_size
    events = usage.read_events()
    err, path, status = events
    assert err["status"] == "error" and err["error"].startswith("ValueError")
    assert path["result_size"] == 5              # nodes + edges
    assert "result_size" not in status           # nothing countable


def test_disabled_records_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AURYN_USAGE_STATS", "0")
    import auryn.core.usage as u
    u = importlib.reload(u)
    u.record_call(graph="g", tool="locate", op="read", status="ok", duration_ms=1)
    assert u.read_events() == []
    assert not u.usage_log_path().exists()


def test_summarize_aggregates(usage):
    for _ in range(3):
        usage.record_call(graph="g", tool="who_calls", op="read", status="ok", duration_ms=10,
                          result={"callers": [1]})
    usage.record_call(graph="g", tool="who_calls", op="read", status="error", duration_ms=20)
    usage.record_call(graph="g", tool="index_repository", op="write", status="ok", duration_ms=100)

    s = usage.summarize(usage.read_events())
    assert s["total"] == 5
    assert s["by_op"] == {"read": 4, "write": 1}
    assert s["errors"] == 1 and s["sessions"] == 1
    wc = s["by_tool"]["who_calls"]
    assert wc["count"] == 4 and wc["errors"] == 1 and wc["op"] == "read"
    assert wc["result"] == 3 and wc["result_n"] == 3   # 3 successful calls carried a result_size


def test_read_events_filters(usage):
    usage.record_call(graph="alpha", tool="locate", op="read", status="ok", duration_ms=1)
    usage.record_call(graph="beta", tool="locate", op="read", status="ok", duration_ms=1)
    assert {e["graph"] for e in usage.read_events(graph="alpha")} == {"alpha"}
    # a far-future cutoff excludes everything; a far-past window keeps it
    assert usage.read_events(since="2999-01-01T00:00:00+00:00") == []
    assert len(usage.read_events(since="3650d")) == 2


def test_parse_since_relative(usage):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    assert usage.parse_since("24h") < now          # a window in the past
    assert usage.parse_since("2026-01-01") == "2026-01-01"  # non-relative passes through
