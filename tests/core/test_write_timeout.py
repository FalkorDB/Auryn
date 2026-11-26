"""#857: the deep (semantic) pass died mid-write with FalkorDB's `Query timed out` because write
queries inherited the server's 1s default TIMEOUT, which a large batch exceeds under host load.
Profiling showed the batched MERGE is an index-seek, ~25ms for 2000 rows, degree-independent -- not
a slow query -- so the fix is to give writes the same generous per-query timeout the reads already
carry, plus a split-and-retry fallback for a server that pins TIMEOUT_MAX below a batch's need.

Pure-unit over a fake Graph that records each query's `timeout=` and can simulate a timeout on
oversized batches -- no DB needed."""

from __future__ import annotations

import pytest
from redis.exceptions import ResponseError

from auryn.core.graph_client import _WRITE_QUERY_TIMEOUT_MS, GraphWriter
from auryn.core.graph_model import GraphEdge, GraphNode


class _FakeResult:
    def __init__(self, result_set):
        self.result_set = result_set


class _RecordingGraph:
    """Minimal falkordb Graph stand-in. Records every (cypher, params, timeout). A write that
    `RETURN count(...)`s is answered with its row count so merge_edges' matched-count check passes.
    If `timeout_over` is set, any batch whose row list exceeds it raises `Query timed out` (the
    #857 failure), letting us prove the split-and-retry converges."""

    def __init__(self, timeout_over=None, error="Query timed out"):
        self.calls: list[tuple[str, dict, object]] = []
        self.timeout_over = timeout_over
        self.error = error

    def query(self, cypher, params=None, timeout=None):
        params = params or {}
        self.calls.append((cypher, params, timeout))
        rows = params.get("rows") if isinstance(params.get("rows"), list) else params.get("ids")
        rows = rows if isinstance(rows, list) else []
        if self.timeout_over is not None and len(rows) > self.timeout_over:
            raise ResponseError(self.error)
        return _FakeResult([[len(rows)]] if "RETURN count" in cypher else [])

    def ro_query(self, *a, **k):
        return _FakeResult([])

    # successful (non-raising) row-writes, i.e. the leaves that actually landed
    def _landed_rows(self):
        n = 0
        for cypher, params, _ in self.calls:
            rows = params.get("rows") if isinstance(params.get("rows"), list) else params.get("ids")
            if isinstance(rows, list) and (self.timeout_over is None or len(rows) <= self.timeout_over):
                n += len(rows)
        return n


def test_batched_writes_carry_the_generous_timeout():
    g = _RecordingGraph()
    w = GraphWriter(g)
    w.merge_nodes([GraphNode("Function", {"id": "a"}, {"name": "a"})])
    w.merge_edges([GraphEdge("CALLS", "Function", {"id": "a"}, "Function", {"id": "b"}, {"line": 1})])
    assert g.calls
    assert all(timeout == _WRITE_QUERY_TIMEOUT_MS for _, _, timeout in g.calls)  # never the 1s default


def test_single_statement_writes_also_carry_it():
    g = _RecordingGraph()
    w = GraphWriter(g)
    w.merge_node(GraphNode("Function", {"id": "a"}, {"name": "a"}))
    w.set_if("Project", "p", "depth", "deep", "commit_sha", "sha")
    w.delete_node("Function", {"id": "a"})
    assert g.calls and all(timeout == _WRITE_QUERY_TIMEOUT_MS for _, _, timeout in g.calls)


def test_a_timed_out_batch_splits_and_still_writes_every_row():
    # The server rejects any batch over 3 rows as 'Query timed out'. _write_rows must halve down to
    # batches it accepts and land all 10 rows -- the deep pass completes instead of failing.
    g = _RecordingGraph(timeout_over=3)
    w = GraphWriter(g)
    edges = [GraphEdge("CALLS", "Function", {"id": f"a{i}"}, "Function", {"id": f"b{i}"}, {"line": i})
             for i in range(10)]
    w.merge_edges(edges)   # must NOT raise
    assert g._landed_rows() == 10
    # no surviving (landed) write exceeded the server's tolerance
    landed = [p for c, p, _ in g.calls if isinstance(p.get("rows"), list) and len(p["rows"]) <= 3]
    assert landed and all(len(p["rows"]) <= 3 for p in landed)


def test_a_single_row_that_still_times_out_is_reraised():
    # Even one row times out (a genuinely unhealthy server). Splitting bottoms out at size 1 and the
    # error propagates -- now visible (and, via the CLI, exits non-zero) rather than silently shallow.
    g = _RecordingGraph(timeout_over=0)
    w = GraphWriter(g)
    with pytest.raises(ResponseError):
        w.merge_edges([GraphEdge("CALLS", "Function", {"id": "a"}, "Function", {"id": "b"}, {})])


def test_a_non_timeout_error_is_not_split_retried():
    # A different ResponseError (syntax, constraint, ...) must propagate immediately -- splitting a
    # batch would only reissue the same broken query N more times.
    g = _RecordingGraph(timeout_over=3, error="errMsg: syntax error")
    w = GraphWriter(g)
    edges = [GraphEdge("CALLS", "Function", {"id": f"a{i}"}, "Function", {"id": f"b{i}"}, {})
             for i in range(10)]
    with pytest.raises(ResponseError):
        w.merge_edges(edges)
    assert len(g.calls) == 1  # one attempt, no retry storm
