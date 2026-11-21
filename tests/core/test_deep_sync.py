"""The shared deep-pass primitives (`core.deep_sync`) -- above all the overlay's edge-deletion invariant
(`is_name_calls_edge` + `should_drop_name_edge`), which must behave identically for every language's deep
pass. Pinned here directly since a drift would silently corrupt graphs (a real edge dropped, or a wrong
guess kept)."""

from __future__ import annotations

from types import SimpleNamespace

from auryn.core import deep_sync
from auryn.core.graph_model import GraphEdge, GraphNode  # GraphNode: a non-edge item the predicate rejects


def _name_edge(frm: str, to: str) -> GraphEdge:
    return GraphEdge("CALLS", "Function", {"id": frm}, "Function", {"id": to}, {"resolution": "name"})


def _deep(covered, seen):
    return SimpleNamespace(covered_ids=set(covered), seen_by_caller={k: set(v) for k, v in seen.items()})


def test_is_name_calls_edge_matches_only_fast_name_calls():
    assert deep_sync.is_name_calls_edge(_name_edge("a", "b")) is True
    # a resolved (deep) CALLS edge is never a drop candidate
    assert deep_sync.is_name_calls_edge(
        GraphEdge("CALLS", "Function", {"id": "a"}, "Function", {"id": "b"}, {"resolution": "resolved"})) is False
    # a non-CALLS edge and a node are not name-CALLS edges
    assert deep_sync.is_name_calls_edge(
        GraphEdge("REFERENCES", "Function", {"id": "a"}, "Type", {"id": "t"}, {"resolution": "name"})) is False
    assert deep_sync.is_name_calls_edge(GraphNode("Function", {"id": "a"}, {})) is False


def test_should_drop_name_edge_drops_only_an_identified_in_project_call():
    loc_by_id = {"b": SimpleNamespace(name="b")}  # the callee is a known project callable
    deep = _deep(covered={"a"}, seen={"a": {"b"}})  # deep covered caller a and identified call `b`
    assert deep_sync.should_drop_name_edge(_name_edge("a", "b"), deep, loc_by_id) is True


def test_should_drop_name_edge_keeps_what_deep_did_not_identify():
    loc_by_id = {"b": SimpleNamespace(name="b")}
    # caller not covered by deep -> keep (deep didn't analyze it)
    assert deep_sync.should_drop_name_edge(_name_edge("a", "b"), _deep(set(), {"a": {"b"}}), loc_by_id) is False
    # deep covered the caller but did NOT identify this call (ambiguous def / fn-pointer) -> keep
    assert deep_sync.should_drop_name_edge(_name_edge("a", "b"), _deep({"a"}, {"a": set()}), loc_by_id) is False
    # the target is not a known project callable (extern / other-repo: loc_by_id miss) -> keep
    assert deep_sync.should_drop_name_edge(_name_edge("a", "x"), _deep({"a"}, {"a": {"x"}}), loc_by_id) is False
