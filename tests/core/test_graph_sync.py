"""Pure unit tests for fingerprint() and compute_delta() -- no FalkorDB, no git,
just GraphNode/GraphEdge lists in and a GraphDelta out."""

from __future__ import annotations

from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.core.graph_sync import compute_delta, edge_key, fingerprint, node_key


def _node(id_, **props):
    return GraphNode(label="Class", key={"id": id_}, properties=props)


def _edge(from_id, to_id, **props):
    return GraphEdge(
        rel_type="DEPENDS_ON", from_label="Class", from_key={"id": from_id},
        to_label="Class", to_key={"id": to_id}, properties=props,
    )


def _current_nodes(nodes):
    """Builds the (key -> fingerprint) shape read_fingerprints() would have
    produced for these nodes, as if they were already sitting in the graph."""
    return {node_key(n): fingerprint(n.properties) for n in nodes}


def _current_edges(edges):
    return {edge_key(e): fingerprint(e.properties) for e in edges}


def test_fingerprint_is_deterministic_regardless_of_key_order():
    assert fingerprint({"name": "A", "kind": "class"}) == fingerprint({"kind": "class", "name": "A"})


def test_fingerprint_differs_for_different_content():
    assert fingerprint({"name": "A"}) != fingerprint({"name": "B"})
    assert fingerprint({}) != fingerprint({"name": "A"})


def test_add_only():
    delta = compute_delta({}, {}, target=[_node("a", name="A")])
    assert [n.key["id"] for n in delta.nodes_to_upsert] == ["a"]
    assert delta.nodes_to_remove == []


def test_remove_only():
    delta = compute_delta(_current_nodes([_node("a", name="A")]), {}, target=[])
    assert delta.nodes_to_upsert == []
    assert delta.nodes_to_remove == [("Class", {"id": "a"})]


def test_unchanged_node_is_neither_upserted_nor_removed():
    node = _node("a", name="A")
    delta = compute_delta(_current_nodes([node]), {}, target=[_node("a", name="A")])
    assert delta.nodes_to_upsert == []
    assert delta.nodes_to_remove == []


def test_property_change_is_an_upsert_not_a_remove_plus_add():
    delta = compute_delta(_current_nodes([_node("a", name="Old")]), {}, target=[_node("a", name="New")])
    assert [n.properties["name"] for n in delta.nodes_to_upsert] == ["New"]
    assert delta.nodes_to_remove == []


def test_edges_add_remove_and_property_change():
    current_edges = [_edge("a", "b", kind="CALL"), _edge("x", "y", kind="CALL")]
    delta = compute_delta(
        {},
        _current_edges(current_edges),
        target=[_edge("a", "b", kind="TYPE_REFERENCE"), _edge("p", "q", kind="CALL")],
    )
    upserted = {(e.from_key["id"], e.to_key["id"]): e.properties["kind"] for e in delta.edges_to_upsert}
    assert upserted == {("a", "b"): "TYPE_REFERENCE", ("p", "q"): "CALL"}
    assert delta.edges_to_remove == [("DEPENDS_ON", "Class", {"id": "x"}, "Class", {"id": "y"})]


def test_mixed_nodes_and_edges_in_one_pass():
    current_nodes = [_node("a", name="A"), _node("b", name="B")]
    current_edges = [_edge("a", "b")]
    target = [_node("a", name="A"), _node("c", name="C")]  # b removed, c added, edge a->b removed

    delta = compute_delta(_current_nodes(current_nodes), _current_edges(current_edges), target)

    assert [n.key["id"] for n in delta.nodes_to_upsert] == ["c"]
    assert delta.nodes_to_remove == [("Class", {"id": "b"})]
    assert delta.edges_to_upsert == []
    assert delta.edges_to_remove == [("DEPENDS_ON", "Class", {"id": "a"}, "Class", {"id": "b"})]


def test_diff_is_symmetric_swapping_arguments_gives_the_inverse():
    old_state = [_node("a", name="A"), _node("b", name="B")]
    new_state = [_node("a", name="A-renamed"), _node("c", name="C")]

    forward = compute_delta(_current_nodes(old_state), {}, target=new_state)
    backward = compute_delta(_current_nodes(new_state), {}, target=old_state)

    assert {n.key["id"] for n in forward.nodes_to_upsert} == {"a", "c"}
    assert forward.nodes_to_remove == [("Class", {"id": "b"})]

    assert {n.key["id"] for n in backward.nodes_to_upsert} == {"a", "b"}
    assert backward.nodes_to_remove == [("Class", {"id": "c"})]
