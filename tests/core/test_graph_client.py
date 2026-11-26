"""Unit tests for GraphWriter's deletion/subgraph-read/apply_delta additions --
the first real deletion capability added to this project (every ingestor so far
only ever adds/updates via MERGE). Uses the real `graph` fixture from
tests/conftest.py."""

from __future__ import annotations

import pytest

from auryn.core.graph_client import GraphWriter, _edge_fingerprint_query, _node_fingerprint_query
from auryn.core.graph_model import GraphEdge, GraphNode, NodeSchema
from auryn.core.graph_sync import (
    GraphDelta,
    compute_delta,
    edge_key,
    fingerprint,
    node_key,
    stream_apply,
)


def test_get_property_returns_none_when_node_or_property_missing(graph):
    writer = GraphWriter(graph)
    # Establish the graph key first (ro_query on a graph nothing has ever been
    # written to raises rather than just returning empty -- every real caller
    # goes through setup_schema()/merge_node() first, same as every ingestor).
    writer.merge_node(GraphNode("Project", {"id": "other"}, {}))

    assert writer.get_property("Project", {"id": "missing"}, "commit_sha") is None

    writer.merge_node(GraphNode("Project", {"id": "p"}, {"name": "P"}))
    assert writer.get_property("Project", {"id": "p"}, "commit_sha") is None


def test_get_property_returns_the_value_when_present(graph):
    writer = GraphWriter(graph)
    writer.merge_node(GraphNode("Project", {"id": "p"}, {"commit_sha": "abc123"}))
    assert writer.get_property("Project", {"id": "p"}, "commit_sha") == "abc123"


def test_delete_node_removes_it_and_its_edges(graph):
    writer = GraphWriter(graph)
    writer.merge_node(GraphNode("Class", {"id": "a"}, {"name": "A"}))
    writer.merge_node(GraphNode("Class", {"id": "b"}, {"name": "B"}))
    writer.merge_edge(GraphEdge("EXTENDS", "Class", {"id": "a"}, "Class", {"id": "b"}))

    writer.delete_node("Class", {"id": "a"})

    assert graph.query("MATCH (n:Class {id: 'a'}) RETURN n").result_set == []
    assert graph.query("MATCH ()-[r:EXTENDS]->() RETURN r").result_set == []
    assert graph.query("MATCH (n:Class {id: 'b'}) RETURN n").result_set != []


def test_delete_edge_removes_only_that_edge(graph):
    writer = GraphWriter(graph)
    writer.merge_node(GraphNode("Class", {"id": "a"}, {}))
    writer.merge_node(GraphNode("Class", {"id": "b"}, {}))
    writer.merge_edge(GraphEdge("EXTENDS", "Class", {"id": "a"}, "Class", {"id": "b"}))

    writer.delete_edge("EXTENDS", "Class", {"id": "a"}, "Class", {"id": "b"})

    assert graph.query("MATCH ()-[r:EXTENDS]->() RETURN r").result_set == []
    assert graph.query("MATCH (n:Class {id: 'a'}) RETURN n").result_set != []
    assert graph.query("MATCH (n:Class {id: 'b'}) RETURN n").result_set != []


def test_read_fingerprints_scopes_by_id_prefix(graph):
    writer = GraphWriter(graph)
    writer.merge_node(GraphNode("Class", {"id": "csharp://proj/a"}, {"name": "A"}))
    writer.merge_node(GraphNode("Class", {"id": "csharp://proj/b"}, {"name": "B"}))
    writer.merge_edge(GraphEdge("EXTENDS", "Class", {"id": "csharp://proj/a"}, "Class", {"id": "csharp://proj/b"}))
    # an unrelated project's node in the same graph -- must never show up
    writer.merge_node(GraphNode("Table", {"id": "oracle://other/t"}, {"name": "Other"}))

    node_fps, edge_fps = writer.read_fingerprints("csharp://proj", ["Class"])

    key_a = node_key(GraphNode("Class", {"id": "csharp://proj/a"}, {}))
    key_b = node_key(GraphNode("Class", {"id": "csharp://proj/b"}, {}))
    assert set(node_fps) == {key_a, key_b}
    assert node_fps[key_a] == fingerprint({"name": "A"})
    assert node_fps[key_b] == fingerprint({"name": "B"})

    expected_edge_key = edge_key(
        GraphEdge("EXTENDS", "Class", {"id": "csharp://proj/a"}, "Class", {"id": "csharp://proj/b"})
    )
    assert set(edge_fps) == {expected_edge_key}
    assert edge_fps[expected_edge_key] == fingerprint({})


def test_read_fingerprints_never_reads_a_sibling_project(graph):
    """`python://api` is a string prefix of `python://api2/...`. The read owns the project id and
    the ids that continue it at a `/` or `.` boundary -- a top-level `<proj>.func.f()` as much as
    a `<proj>/file/...` -- and nothing of a sibling whose name merely starts with it."""
    writer = GraphWriter(graph)
    writer.merge_node(GraphNode("Project", {"id": "python://api"}, {"name": "api"}))
    writer.merge_node(GraphNode("Class", {"id": "python://api/a"}, {"name": "A"}))
    writer.merge_node(GraphNode("Class", {"id": "python://api.class.top"}, {"name": "Top"}))
    writer.merge_edge(GraphEdge("HAS_CLASS", "Project", {"id": "python://api"}, "Class", {"id": "python://api/a"}))
    # the sibling: same shape, a longer name, plus an edge into it that isn't `api`'s to diff
    writer.merge_node(GraphNode("Project", {"id": "python://api2"}, {"name": "api2"}))
    writer.merge_node(GraphNode("Class", {"id": "python://api2/a"}, {"name": "A"}))
    writer.merge_edge(GraphEdge("HAS_CLASS", "Project", {"id": "python://api2"}, "Class", {"id": "python://api2/a"}))
    writer.merge_edge(GraphEdge("EXTENDS", "Class", {"id": "python://api/a"}, "Class", {"id": "python://api2/a"}))

    node_fps, edge_fps = writer.read_fingerprints("python://api", ["Project", "Class"])

    assert set(node_fps) == {
        node_key(GraphNode("Project", {"id": "python://api"}, {})),
        node_key(GraphNode("Class", {"id": "python://api/a"}, {})),
        node_key(GraphNode("Class", {"id": "python://api.class.top"}, {})),
    }
    assert set(edge_fps) == {
        edge_key(GraphEdge("HAS_CLASS", "Project", {"id": "python://api"}, "Class", {"id": "python://api/a"}))
    }


def test_setup_schema_raises_resultset_size(graph):
    """FalkorDB's RESULTSET_SIZE config defaults to 10000 and silently truncates
    any query result past that many rows -- confirmed empirically (a 15000-row
    MATCH...RETURN with no LIMIT/ORDER BY/SKIP at all still came back with exactly
    10000 rows, no error or warning). A real git_sync diff against a large
    codebase (dotnet/aspnetcore has ~271k DEPENDS_ON edges from Method alone)
    blows straight through this default, which is what actually caused a
    benchmark run to report an absurd nodes_added count with zero nodes_removed."""
    writer = GraphWriter(graph)
    writer.merge_node(GraphNode("Project", {"id": "p"}, {}))  # establish the graph key so teardown can delete it
    writer.setup_schema([])

    assert graph.execute_command("GRAPH.CONFIG", "GET", "RESULTSET_SIZE") == ["RESULTSET_SIZE", 1_000_000]


def test_read_fingerprints_reads_past_falkordbs_default_resultset_size_cap(graph):
    """End-to-end: setup_schema() raises RESULTSET_SIZE (see above), so
    read_fingerprints -- which issues one plain, unordered query per label rather
    than paginating -- still comes back with everything rather than silently
    truncating at 10000."""
    writer = GraphWriter(graph)
    writer.setup_schema([NodeSchema(label="Class")])
    graph.query(
        "UNWIND range(1, 10001) AS i CREATE (:Class {id: 'csharp://proj/' + toString(i), name: 'C' + toString(i)})"
    )

    node_fps, _ = writer.read_fingerprints("csharp://proj", ["Class"])

    expected_keys = {node_key(GraphNode("Class", {"id": f"csharp://proj/{i}"}, {})) for i in range(1, 10002)}
    assert len(node_fps) == 10001
    assert set(node_fps) == expected_keys


def test_read_fingerprints_never_reads_a_project_whose_name_continues_at_a_dot(graph):
    """`Acme.Core` and `Acme.Core.Tests` are two projects, yet every id of the second continues
    the first at a `.` -- shaped just like `Acme.Core`'s own top-level `<proj>.class.x`. The
    second's Project node is what tells them apart: what it owns is left out, and `Acme.Core`'s
    own dotted ids are still read."""
    writer = GraphWriter(graph)
    core, tests = "csharp://Sol/Acme.Core", "csharp://Sol/Acme.Core.Tests"
    for pid in (core, tests):
        writer.merge_node(GraphNode("Project", {"id": pid}, {"name": pid.rsplit("/", 1)[1]}))
        writer.merge_node(GraphNode("Class", {"id": f"{pid}/ns/A"}, {"name": "A"}))
        writer.merge_node(GraphNode("Class", {"id": f"{pid}.class.Top"}, {"name": "Top"}))
        writer.merge_edge(GraphEdge("HAS_CLASS", "Project", {"id": pid}, "Class", {"id": f"{pid}/ns/A"}))
    # an edge into the other project isn't `Acme.Core`'s to diff either
    writer.merge_edge(GraphEdge("EXTENDS", "Class", {"id": f"{core}/ns/A"}, "Class", {"id": f"{tests}/ns/A"}))

    for pid in (core, tests):
        node_fps, edge_fps = writer.read_fingerprints(pid, ["Project", "Class"])

        assert set(node_fps) == {
            node_key(GraphNode("Project", {"id": pid}, {})),
            node_key(GraphNode("Class", {"id": f"{pid}/ns/A"}, {})),
            node_key(GraphNode("Class", {"id": f"{pid}.class.Top"}, {})),
        }
        assert set(edge_fps) == {
            edge_key(GraphEdge("HAS_CLASS", "Project", {"id": pid}, "Class", {"id": f"{pid}/ns/A"}))
        }


def test_read_fingerprints_leaves_out_a_nested_project_created_while_it_reads(graph, monkeypatch):
    """Another session ingests `Acme.Core.Tests` for the first time while `Acme.Core` resyncs. The
    nested Project ids are looked up once, after every read has its rows, so a project whose nodes
    a read returned is already known to be someone else's -- not `Acme.Core`'s leftovers."""
    writer, other = GraphWriter(graph), GraphWriter(graph)
    core, tests = "csharp://Sol/Acme.Core", "csharp://Sol/Acme.Core.Tests"
    writer.merge_node(GraphNode("Project", {"id": core}, {"name": "Acme.Core"}))
    writer.merge_node(GraphNode("Class", {"id": f"{core}/ns/A"}, {"name": "A"}))
    ro_query, lookups = graph.ro_query, []

    def a_sibling_appears_before_the_first_read(query, *args, **kwargs):
        if query.startswith("MATCH (p:Project)"):
            lookups.append(query)
        if "_fingerprint" in query and not other.node_exists("Project", tests):
            # its Project node first, as a language ingestor's sync writes it
            other.merge_node(GraphNode("Project", {"id": tests}, {"name": "Acme.Core.Tests"}))
            other.merge_node(GraphNode("Class", {"id": f"{tests}/ns/A"}, {"name": "A"}))
        return ro_query(query, *args, **kwargs)

    monkeypatch.setattr(graph, "ro_query", a_sibling_appears_before_the_first_read)

    node_fps, _ = writer.read_fingerprints(core, ["Project", "Class"])

    assert set(node_fps) == {
        node_key(GraphNode("Project", {"id": core}, {})),
        node_key(GraphNode("Class", {"id": f"{core}/ns/A"}, {})),
    }
    assert len(lookups) == 1  # once per resync, not once per label read


def test_subgraph_fingerprint_queries_plan_as_a_label_scan_not_a_full_scan(graph):
    """An earlier, unlabeled version of these queries (`MATCH (n) WHERE
    n.id STARTS WITH ...`) planned as "All Node Scan" regardless of the WHERE
    clause -- FalkorDB's indexes are per-label, so an unlabeled match can't
    consult one no matter how selective the filter is. Confirmed (via
    GRAPH.EXPLAIN) to be why a real git_sync diff against dotnet/aspnetcore timed
    out: at real scale (multiple relationship types, hundreds of thousands of
    edges) an unindexed full scan's cost climbed past FalkorDB's default 1000ms
    query timeout. This asserts the fix (querying per-label, so only that label's
    nodes are ever visited) rather than just trusting the Cypher text looks right.

    It is a label scan with an exact id filter, not a range-index scan: the range
    `[prefix, prefix + chr(0xFFFF))` is not a project boundary and FalkorDB 4.x
    answers it wrongly for mixed-case ids (see graph_client._owned_ids)."""
    writer = GraphWriter(graph)
    writer.setup_schema([NodeSchema(label="Class")])

    node_plan = str(graph.explain(_node_fingerprint_query("Class"), {"own": "a", "sub": "a/", "dot": "a."}))
    assert "Node By Label Scan" in node_plan
    assert "All Node Scan" not in node_plan

    edge_plan = str(graph.explain(_edge_fingerprint_query("Class"), {"own": "a", "sub": "a/", "dot": "a."}))
    assert "Node By Label Scan" in edge_plan
    assert "All Node Scan" not in edge_plan


def test_merge_node_stores_a_fingerprint_that_changes_with_properties(graph):
    """The whole point of _fingerprint: read_fingerprints can tell "unchanged"
    from "changed" without ever fetching full properties back."""
    writer = GraphWriter(graph)
    writer.merge_node(GraphNode("Class", {"id": "a"}, {"name": "A"}))

    node_fps, _ = writer.read_fingerprints("a", ["Class"])
    key = node_key(GraphNode("Class", {"id": "a"}, {}))
    assert node_fps[key] == fingerprint({"name": "A"})

    # re-merging with identical properties: fingerprint stays the same
    writer.merge_node(GraphNode("Class", {"id": "a"}, {"name": "A"}))
    node_fps_again, _ = writer.read_fingerprints("a", ["Class"])
    assert node_fps_again[key] == node_fps[key]

    # merging with different properties: fingerprint changes
    writer.merge_node(GraphNode("Class", {"id": "a"}, {"name": "Renamed"}))
    node_fps_changed, _ = writer.read_fingerprints("a", ["Class"])
    assert node_fps_changed[key] == fingerprint({"name": "Renamed"})
    assert node_fps_changed[key] != node_fps[key]


def test_null_valued_property_round_trips_through_compute_delta_correctly(graph):
    """Cypher's `SET n.k = null` (and the map-merge `SET n += {...}` the batched
    writers use) both *delete* that property rather than storing a null -- confirmed
    empirically for both write shapes. This used to make the pre-fingerprint diff
    (which compared FalkorDB's actual properties(n) against a freshly computed
    dict that still has the None key) see a spurious "changed" on every single
    sync for any node with a None-valued property (e.g. an Enum with no explicit
    underlying_type) -- discovered via a real benchmark run reporting 7082
    nodes_updated for a 9-file diff. fingerprint() sidesteps this by never
    comparing against what FalkorDB actually persisted; this test locks that in
    directly rather than trusting the reasoning."""
    writer = GraphWriter(graph)
    node_id = "e1"
    key = node_key(GraphNode("Enum", {"id": node_id}, {}))

    # v1: write with a None-valued property
    writer.merge_nodes([GraphNode("Enum", {"id": node_id}, {"name": "Color", "underlying_type": None})])
    stored = graph.query(f"MATCH (n:Enum {{id: '{node_id}'}}) RETURN properties(n)").result_set[0][0]
    assert "underlying_type" not in stored  # FalkorDB actually dropped it

    fps_v1, _ = writer.read_fingerprints(node_id, ["Enum"])

    # v2: re-diff against an IDENTICAL target (still None) -- must be "unchanged"
    delta = compute_delta(fps_v1, {}, [GraphNode("Enum", {"id": node_id}, {"name": "Color", "underlying_type": None})])
    assert delta.nodes_to_upsert == []

    # v3: diff against None -> a real value -- must be "changed"
    delta = compute_delta(fps_v1, {}, [GraphNode("Enum", {"id": node_id}, {"name": "Color", "underlying_type": "byte"})])
    assert len(delta.nodes_to_upsert) == 1
    writer.apply_delta(delta)
    stored_v3 = graph.query(f"MATCH (n:Enum {{id: '{node_id}'}}) RETURN properties(n)").result_set[0][0]
    assert stored_v3["underlying_type"] == "byte"

    # v4: diff back from the real value to None -- must be "changed" again, and
    # the property must actually disappear from storage (not linger stale)
    fps_v3, _ = writer.read_fingerprints(node_id, ["Enum"])
    delta = compute_delta(fps_v3, {}, [GraphNode("Enum", {"id": node_id}, {"name": "Color", "underlying_type": None})])
    assert len(delta.nodes_to_upsert) == 1
    writer.apply_delta(delta)
    stored_v4 = graph.query(f"MATCH (n:Enum {{id: '{node_id}'}}) RETURN properties(n)").result_set[0][0]
    assert "underlying_type" not in stored_v4

    fps_v4, _ = writer.read_fingerprints(node_id, ["Enum"])
    assert fps_v4[key] == fps_v1[key]  # back to the original None-state fingerprint


def test_null_valued_edge_property_round_trips_through_compute_delta_correctly(graph):
    """Same as the node version above, but for edges -- a real, live case: the SQL
    ingestor's REFERENCES edge (foreign keys) carries constraint_name/on_delete/
    on_update, all of which are None whenever the FK is unnamed or has no ON
    DELETE/ON UPDATE clause (the common case). Confirmed separately that `SET r +=
    {...}` on a relationship drops a null-valued key exactly like it does on a
    node, so this needs its own test rather than assuming node behavior carries
    over."""
    writer = GraphWriter(graph)
    writer.merge_nodes([GraphNode("Column", {"id": "p://db/a"}, {}), GraphNode("Column", {"id": "p://db/b"}, {})])
    key = edge_key(GraphEdge("REFERENCES", "Column", {"id": "p://db/a"}, "Column", {"id": "p://db/b"}))

    def _references(constraint_name):
        return GraphEdge(
            "REFERENCES", "Column", {"id": "p://db/a"}, "Column", {"id": "p://db/b"},
            {"constraint_name": constraint_name, "on_delete": "CASCADE"},
        )

    writer.merge_edges([_references(None)])
    stored = graph.query("MATCH ()-[r:REFERENCES]->() RETURN properties(r)").result_set[0][0]
    assert "constraint_name" not in stored

    _, fps_v1 = writer.read_fingerprints("p://db", ["Column"])

    delta = compute_delta({}, fps_v1, [_references(None)])
    assert delta.edges_to_upsert == []  # unnamed FK re-synced unchanged: no spurious update

    delta = compute_delta({}, fps_v1, [_references("fk_1")])
    assert len(delta.edges_to_upsert) == 1
    writer.apply_delta(delta)
    stored_named = graph.query("MATCH ()-[r:REFERENCES]->() RETURN properties(r)").result_set[0][0]
    assert stored_named["constraint_name"] == "fk_1"

    _, fps_v2 = writer.read_fingerprints("p://db", ["Column"])
    delta = compute_delta({}, fps_v2, [_references(None)])
    assert len(delta.edges_to_upsert) == 1
    writer.apply_delta(delta)
    stored_unnamed_again = graph.query("MATCH ()-[r:REFERENCES]->() RETURN properties(r)").result_set[0][0]
    assert "constraint_name" not in stored_unnamed_again

    _, fps_v3 = writer.read_fingerprints("p://db", ["Column"])
    assert fps_v3[key] == fps_v1[key]


def test_serialized_node_properties_never_include_fingerprint():
    """graph_client writes _fingerprint as a real stored property (needed so
    read_fingerprints can fetch it back later) -- callers' job is to hide it
    again, since it's internal bookkeeping, not a real domain property. This
    behavior actually lives in core/graph_queries.py; asserted here too since
    it's a direct consequence of graph_client storing an underscore-prefixed
    property that must never leak into the properties(n) surface tests
    elsewhere assert against."""
    from auryn.core.graph_queries import _visible_properties

    assert _visible_properties({"name": "A", "_fingerprint": "abc123"}) == {"name": "A"}


def test_merge_nodes_batches_across_mixed_labels(graph):
    writer = GraphWriter(graph)
    writer.merge_nodes(
        [
            GraphNode("Class", {"id": "a"}, {"name": "A"}),
            GraphNode("Method", {"id": "m1"}, {"name": "M1"}),
            GraphNode("Class", {"id": "b"}, {"name": "B"}),
        ]
    )

    assert graph.query("MATCH (n:Class) RETURN n.id, n.name ORDER BY n.id").result_set == [["a", "A"], ["b", "B"]]
    assert graph.query("MATCH (n:Method {id: 'm1'}) RETURN n.name").result_set == [["M1"]]


def test_merge_nodes_chunks_beyond_one_write_batch(graph):
    writer = GraphWriter(graph)
    nodes = [GraphNode("Class", {"id": f"c{i}"}, {"n": i}) for i in range(2001)]

    writer.merge_nodes(nodes)

    assert graph.query("MATCH (n:Class) RETURN count(n)").result_set == [[2001]]


def test_merge_edges_skips_rows_with_a_missing_endpoint(graph, caplog):
    writer = GraphWriter(graph)
    writer.merge_nodes([GraphNode("Class", {"id": "a"}, {}), GraphNode("Class", {"id": "b"}, {})])

    with caplog.at_level("WARNING"):
        writer.merge_edges(
            [
                GraphEdge("EXTENDS", "Class", {"id": "a"}, "Class", {"id": "b"}),
                GraphEdge("EXTENDS", "Class", {"id": "a"}, "Class", {"id": "missing"}),
            ]
        )

    assert graph.query("MATCH ()-[r:EXTENDS]->() RETURN count(r)").result_set == [[1]]
    assert "missing endpoint" in caplog.text


def test_delete_nodes_batches_across_mixed_labels(graph):
    writer = GraphWriter(graph)
    writer.merge_nodes(
        [
            GraphNode("Class", {"id": "a"}, {}),
            GraphNode("Class", {"id": "b"}, {}),
            GraphNode("Method", {"id": "m1"}, {}),
        ]
    )

    writer.delete_nodes([("Class", {"id": "a"}), ("Method", {"id": "m1"})])

    assert graph.query("MATCH (n:Class) RETURN n.id").result_set == [["b"]]
    assert graph.query("MATCH (n:Method) RETURN n").result_set == []


def test_delete_edges_removes_only_the_given_edges(graph):
    writer = GraphWriter(graph)
    writer.merge_nodes([GraphNode("Class", {"id": "a"}, {}), GraphNode("Class", {"id": "b"}, {})])
    writer.merge_edges(
        [
            GraphEdge("EXTENDS", "Class", {"id": "a"}, "Class", {"id": "b"}),
            GraphEdge("IMPLEMENTS", "Class", {"id": "a"}, "Class", {"id": "b"}),
        ]
    )

    writer.delete_edges([("EXTENDS", "Class", {"id": "a"}, "Class", {"id": "b"})])

    assert graph.query("MATCH ()-[r:EXTENDS]->() RETURN r").result_set == []
    assert graph.query("MATCH ()-[r:IMPLEMENTS]->() RETURN r").result_set != []


def test_apply_delta_upserts_and_removes_in_one_call(graph):
    writer = GraphWriter(graph)
    writer.merge_node(GraphNode("Class", {"id": "a"}, {"name": "Old"}))
    writer.merge_node(GraphNode("Class", {"id": "stale"}, {}))
    writer.merge_edge(GraphEdge("EXTENDS", "Class", {"id": "a"}, "Class", {"id": "stale"}))

    delta = GraphDelta(
        nodes_to_upsert=[GraphNode("Class", {"id": "a"}, {"name": "New"}), GraphNode("Class", {"id": "b"}, {})],
        nodes_to_remove=[("Class", {"id": "stale"})],
        edges_to_upsert=[GraphEdge("IMPLEMENTS", "Class", {"id": "a"}, "Class", {"id": "b"})],
        edges_to_remove=[("EXTENDS", "Class", {"id": "a"}, "Class", {"id": "stale"})],
    )
    writer.apply_delta(delta)

    assert graph.query("MATCH (n:Class {id: 'a'}) RETURN n.name").result_set == [["New"]]
    assert graph.query("MATCH (n:Class {id: 'stale'}) RETURN n").result_set == []
    assert graph.query("MATCH (:Class {id: 'a'})-[r:IMPLEMENTS]->(:Class {id: 'b'}) RETURN r").result_set != []
    assert graph.query("MATCH ()-[r:EXTENDS]->() RETURN r").result_set == []


def test_apply_delta_stamps_the_commit_last(graph, monkeypatch):
    """The Project node's `commit_sha` is what the next sync short-circuits on ("already at this
    commit"). It is cleared before the delta is written and set after it: an apply that dies
    midway must leave NO commit -- neither the new one nor the old one -- over a half-written graph."""
    writer = GraphWriter(graph)
    writer.merge_node(GraphNode("Project", {"id": "p"}, {"name": "p", "commit_sha": "c1"}))
    delta = GraphDelta(
        nodes_to_upsert=[
            GraphNode("Project", {"id": "p"}, {"name": "p", "commit_sha": "c2"}),
            GraphNode("Class", {"id": "p/a"}, {}),
        ],
        edges_to_upsert=[GraphEdge("HAS_CLASS", "Project", {"id": "p"}, "Class", {"id": "p/a"})],
    )

    def killed(edges):
        raise RuntimeError("killed mid-apply")

    with monkeypatch.context() as m:
        m.setattr(writer, "merge_edges", killed)
        with pytest.raises(RuntimeError):
            writer.apply_delta(delta)
    assert writer.get_property("Project", {"id": "p"}, "commit_sha") is None  # not c2, and not c1 either

    writer.apply_delta(delta)  # the healthy re-run completes the delta, and only then stamps
    assert writer.get_property("Project", {"id": "p"}, "commit_sha") == "c2"
    assert graph.query("MATCH (:Project {id: 'p'})-[r:HAS_CLASS]->(:Class {id: 'p/a'}) RETURN count(r)").result_set == [[1]]


# -- stream_apply: memory-bounded streaming counterpart to compute_delta + apply_delta --
#
# Ids are realistic project-prefixed ids (`plsql://Proj...`), not toy ones: read_fingerprints
# reads exactly what the project owns -- its id and the ids that continue it at a `/` or `.`
# boundary (see graph_client._owned_ids) -- so a toy id that isn't shaped that way is never read.

_STREAM_LABELS = ["Class", "Method"]
_P = "plsql://Proj"  # id prefix passed to stream_apply / read_fingerprints


def _stream_schemas():
    return [NodeSchema("Class"), NodeSchema("Method")]


def _fresh(items):
    """A make_target callable: a fresh iterator over `items` each call (stream_apply
    iterates it twice -- a nodes pass, then an edges pass)."""
    return lambda: iter(list(items))


def test_stream_apply_first_ingest_writes_everything_with_accurate_counts(graph):
    writer = GraphWriter(graph)
    writer.setup_schema(_stream_schemas())
    items = [
        GraphNode("Class", {"id": f"{_P}.a"}, {"name": "A"}),
        GraphNode("Method", {"id": f"{_P}.a.m"}, {"name": "m"}),
        GraphEdge("HAS_METHOD", "Class", {"id": f"{_P}.a"}, "Method", {"id": f"{_P}.a.m"}),
    ]

    stats, reals, placeholders = stream_apply(writer, _P, _STREAM_LABELS, _fresh(items))

    assert (stats.nodes_added, stats.edges_added) == (2, 1)
    assert (stats.nodes_updated, stats.nodes_removed, stats.edges_updated, stats.edges_removed) == (0, 0, 0, 0)
    assert (reals, placeholders) == ([], [])
    assert graph.query(
        f"MATCH (:Class {{id: '{_P}.a'}})-[:HAS_METHOD]->(:Method {{id: '{_P}.a.m'}}) RETURN 1"
    ).result_set == [[1]]


def test_stream_apply_resync_identical_target_is_a_noop(graph):
    writer = GraphWriter(graph)
    writer.setup_schema(_stream_schemas())
    items = [
        GraphNode("Class", {"id": f"{_P}.a"}, {"name": "A"}),
        GraphNode("Method", {"id": f"{_P}.a.m"}, {"name": "m"}),
        GraphEdge("HAS_METHOD", "Class", {"id": f"{_P}.a"}, "Method", {"id": f"{_P}.a.m"}),
    ]
    stream_apply(writer, _P, _STREAM_LABELS, _fresh(items))

    stats, _, _ = stream_apply(writer, _P, _STREAM_LABELS, _fresh(items))

    assert stats == type(stats)()  # every count zero: nothing added, updated, or removed


def test_stream_apply_incremental_adds_updates_and_removes(graph):
    writer = GraphWriter(graph)
    writer.setup_schema(_stream_schemas())
    v1 = [
        GraphNode("Class", {"id": f"{_P}.a"}, {"name": "Old"}),
        GraphNode("Class", {"id": f"{_P}.stale"}, {}),
        GraphEdge("EXTENDS", "Class", {"id": f"{_P}.a"}, "Class", {"id": f"{_P}.stale"}),
    ]
    stream_apply(writer, _P, _STREAM_LABELS, _fresh(v1))

    v2 = [
        GraphNode("Class", {"id": f"{_P}.a"}, {"name": "New"}),  # property change -> update
        GraphNode("Class", {"id": f"{_P}.b"}, {}),  # new -> add
        GraphEdge("IMPLEMENTS", "Class", {"id": f"{_P}.a"}, "Class", {"id": f"{_P}.b"}),  # new edge
        # {_P}.stale (and the EXTENDS edge to it) are gone -> removals
    ]
    stats, _, _ = stream_apply(writer, _P, _STREAM_LABELS, _fresh(v2))

    assert (stats.nodes_added, stats.nodes_updated, stats.nodes_removed) == (1, 1, 1)
    assert (stats.edges_added, stats.edges_updated, stats.edges_removed) == (1, 0, 1)
    assert graph.query(f"MATCH (n:Class {{id: '{_P}.a'}}) RETURN n.name").result_set == [["New"]]
    assert graph.query(f"MATCH (n:Class {{id: '{_P}.stale'}}) RETURN n").result_set == []
    assert graph.query(
        f"MATCH (:Class {{id: '{_P}.a'}})-[:IMPLEMENTS]->(:Class {{id: '{_P}.b'}}) RETURN 1"
    ).result_set == [[1]]
    assert graph.query("MATCH ()-[r:EXTENDS]->() RETURN r").result_set == []


def test_stream_apply_writes_all_nodes_before_any_edge_regardless_of_target_order(graph):
    """The two-phase (nodes pass, then edges pass) design is what lets an edge be
    written even when the target yields it BEFORE its endpoint nodes -- a naive
    single-pass streaming writer would MATCH nothing and silently drop it."""
    writer = GraphWriter(graph)
    writer.setup_schema(_stream_schemas())
    items = [
        GraphEdge("HAS_METHOD", "Class", {"id": f"{_P}.a"}, "Method", {"id": f"{_P}.a.m"}),  # edge first
        GraphNode("Class", {"id": f"{_P}.a"}, {}),
        GraphNode("Method", {"id": f"{_P}.a.m"}, {}),
    ]

    stats, _, _ = stream_apply(writer, _P, _STREAM_LABELS, _fresh(items))

    assert stats.edges_added == 1
    assert graph.query(
        f"MATCH (:Class {{id: '{_P}.a'}})-[:HAS_METHOD]->(:Method {{id: '{_P}.a.m'}}) RETURN 1"
    ).result_set == [[1]]


@pytest.mark.parametrize("already_synced", [False, True])
def test_stream_apply_interrupted_mid_apply_leaves_the_commit_unstamped(graph, already_synced):
    """A sync that dies between the nodes pass and the edges pass must not leave the Project
    node at the NEW commit -- the next sync would short-circuit on it and never write the rest.
    Nor at the OLD one (a resync): a sync back to that commit would short-circuit just the same."""
    writer = GraphWriter(graph)
    writer.setup_schema([NodeSchema("Project"), NodeSchema("Class")])
    labels = ["Project", "Class"]

    def target(sha):
        return [
            GraphNode("Project", {"id": _P}, {"name": "p", "commit_sha": sha}),
            GraphNode("Class", {"id": f"{_P}.a"}, {"name": sha}),
            GraphEdge("HAS_CLASS", "Project", {"id": _P}, "Class", {"id": f"{_P}.a"}, {"at": sha}),
        ]

    if already_synced:
        stream_apply(writer, _P, labels, _fresh(target("c1")))

    passes = []

    def dies_before_the_edges_pass():  # make_target: fine for the nodes pass, killed on the next
        passes.append(1)
        if len(passes) == 2:
            raise RuntimeError("killed mid-apply")
        return iter(target("c2"))

    with pytest.raises(RuntimeError):
        stream_apply(writer, _P, labels, dies_before_the_edges_pass)
    assert writer.get_property("Project", {"id": _P}, "commit_sha") is None

    stream_apply(writer, _P, labels, _fresh(target("c2")))  # the healthy re-run heals it, then stamps
    assert writer.get_property("Project", {"id": _P}, "commit_sha") == "c2"
    assert graph.query(
        f"MATCH (:Project {{id: '{_P}'}})-[r:HAS_CLASS]->(:Class {{id: '{_P}.a'}}) RETURN r.at"
    ).result_set == [["c2"]]


def test_stream_apply_clears_the_stamp_before_the_first_batch_when_the_project_node_comes_last(graph, monkeypatch):
    """compose and k8s yield their Project node LAST. A full batch of changed nodes ahead of it
    must not reach the graph while the previous commit is still stamped -- a sync that dies there
    would leave that commit claimed over a half-written graph."""
    monkeypatch.setattr("auryn.core.graph_sync._STREAM_BATCH_SIZE", 2)
    writer = GraphWriter(graph)
    writer.setup_schema([NodeSchema("Project"), NodeSchema("Class")])
    labels = ["Project", "Class"]

    def target(sha):
        classes = [GraphNode("Class", {"id": f"{_P}.{c}"}, {"name": sha}) for c in "abc"]
        return [*classes, GraphNode("Project", {"id": _P}, {"name": "p", "commit_sha": sha})]

    stream_apply(writer, _P, labels, _fresh(target("c1")))
    merge_nodes = writer.merge_nodes

    def dies_on_the_first_batch(nodes):
        if nodes[0].label == "Class":
            raise RuntimeError("killed mid-apply")
        merge_nodes(nodes)

    monkeypatch.setattr(writer, "merge_nodes", dies_on_the_first_batch)
    with pytest.raises(RuntimeError):
        stream_apply(writer, _P, labels, _fresh(target("c2")))
    assert writer.get_property("Project", {"id": _P}, "commit_sha") is None

    stream_apply(GraphWriter(graph), _P, labels, _fresh(target("c2")))  # the healthy re-run heals it, then stamps
    assert writer.get_property("Project", {"id": _P}, "commit_sha") == "c2"
    assert graph.query("MATCH (c:Class) RETURN DISTINCT c.name").result_set == [["c2"]]


def test_stream_apply_produces_the_same_graph_as_compute_delta_plus_apply_delta(graph):
    """Parity: streaming and the materialise-then-diff path must leave byte-identical
    graphs (same nodes, edges, and stored properties incl. _fingerprint)."""
    streamed = GraphWriter(graph)
    streamed.setup_schema(_stream_schemas())
    reference_graph = type(graph)(graph.client, graph._name + "_ref")  # sibling graph, same client
    reference = GraphWriter(reference_graph)
    reference.setup_schema(_stream_schemas())

    items = [
        GraphNode("Class", {"id": f"{_P}.a"}, {"name": "A"}),
        GraphNode("Class", {"id": f"{_P}.b"}, {"name": "B"}),
        GraphNode("Method", {"id": f"{_P}.a.m"}, {"name": "m", "static": None}),
        GraphEdge("HAS_METHOD", "Class", {"id": f"{_P}.a"}, "Method", {"id": f"{_P}.a.m"}),
        GraphEdge("EXTENDS", "Class", {"id": f"{_P}.b"}, "Class", {"id": f"{_P}.a"}, {"via": "x"}),
    ]

    stream_apply(streamed, _P, _STREAM_LABELS, _fresh(items))
    n_fps, e_fps = reference.read_fingerprints(_P, _STREAM_LABELS)
    reference.apply_delta(compute_delta(n_fps, e_fps, list(items)))

    def dump(g):
        nodes = g.query("MATCH (n) RETURN labels(n)[0], n.id, properties(n) ORDER BY n.id").result_set
        edges = g.query(
            "MATCH (a)-[r]->(b) RETURN type(r), a.id, b.id, properties(r) ORDER BY a.id, b.id, type(r)"
        ).result_set
        return nodes, edges

    assert dump(graph) == dump(reference_graph)


@pytest.mark.parametrize("name, sibling", [
    ("api", "api2"), ("api", "api.tests"), ("api", "api/core"), ("api.", "api.2"), ("django", "Flask"), ("Flask", "django"),
])
def test_stream_apply_leaves_a_sibling_project_alone(graph, name, sibling):
    """Two projects share a graph and one name is a prefix of the other (`api` / `api2`),
    continues it at a dot (`api` / `api.tests`, as in `Acme.Core` / `Acme.Core.Tests`) or at a
    slash (`api` / `api/core`, as in a C# project `Sol` next to solution `Sol`'s project `Core`),
    does so after a name that itself ends in a dot (`api.` / `api.2`), or differs in letter case
    (`django` / `Flask`: FalkorDB 4.x answers an indexed id range wrongly for those). Resyncing
    the first after it drops two classes must remove those only -- every node and edge of the
    sibling was read back as the first one's own leftovers, and deleted."""
    writer = GraphWriter(graph)
    writer.setup_schema([NodeSchema("Project"), NodeSchema("Class")])

    def project(name, classes):
        # ids continue the project id both ways real ingestors do: `<proj>/file/...`-style ("/")
        # and a top-level `<proj>.class.x` (".") -- e.g. a C++ class outside any namespace.
        pid = f"cpp://{name}"
        items = [GraphNode("Project", {"id": pid}, {"name": name})]
        for cls in classes:
            cid = f"{pid}.class.{cls}" if cls.startswith("top") else f"{pid}/namespace/geo.class.{cls}"
            items.append(GraphNode("Class", {"id": cid}, {"name": cls}))
            items.append(GraphEdge("HAS_CLASS", "Project", {"id": pid}, "Class", {"id": cid}))
        return items

    labels = ["Project", "Class"]
    stream_apply(writer, f"cpp://{name}", labels, _fresh(project(name, ["a", "gone", "top", "top_gone"])))
    stream_apply(writer, f"cpp://{sibling}", labels, _fresh(project(sibling, ["a", "top"])))

    stats, _, _ = stream_apply(writer, f"cpp://{name}", labels, _fresh(project(name, ["a", "top"])))

    # the two dropped classes (one of each id shape) and their HAS_CLASS edges -- nothing else
    assert (stats.nodes_added, stats.nodes_removed, stats.edges_removed) == (0, 2, 2)
    theirs = f"cpp://{sibling}"
    assert graph.query("MATCH (n) WHERE n.id STARTS WITH $p RETURN n.id ORDER BY n.id", {"p": theirs}).result_set == [
        [theirs], [f"{theirs}.class.top"], [f"{theirs}/namespace/geo.class.a"],
    ]
    assert graph.query("MATCH (:Project {id: $p})-[r:HAS_CLASS]->() RETURN count(r)", {"p": theirs}).result_set == [[2]]


def test_set_if_is_a_guarded_compare_and_set(graph):
    from auryn.core.graph_client import GraphWriter
    w = GraphWriter(graph)
    w.query("CREATE (p:Project {id:'x', commit_sha:'A', depth:'fast'})")
    # guard matches -> sets; returns True
    assert w.set_if("Project", "x", "depth", "deep", "commit_sha", "A") is True
    assert w.get_property("Project", {"id": "x"}, "depth") == "deep"
    # guard FAILS (commit moved) -> no-op; returns False, value unchanged
    assert w.set_if("Project", "x", "depth", "fast", "commit_sha", "B") is False
    assert w.get_property("Project", {"id": "x"}, "depth") == "deep"


def test_entity_backfill_is_retryable_after_failure(graph, monkeypatch):
    # #857 review: the :Entity relabel is load-bearing (reads filter MATCH (n:Entity {id})), so its
    # done-marker index must be created ONLY after the relabel succeeds -- a failed relabel must
    # leave NO index, so the next run retries it instead of treating it as done.
    from auryn.core.graph_client import SHARED_NODE_LABEL

    # A pre-:Entity node: create via the RAW graph so the fixture's CREATE-stamp doesn't add the label.
    graph._g.query("CREATE (:Foo {id: 'pre'})")
    writer = GraphWriter(graph)

    def entity_index_present() -> bool:
        rows = graph._g.query(
            "CALL db.indexes() YIELD label, entitytype RETURN label, entitytype"
        ).result_set
        return any(r[0] == SHARED_NODE_LABEL and r[1] == "NODE" for r in rows)

    def has_entity_label() -> bool:
        return graph._g.query(
            "MATCH (n:Foo {id: 'pre'}) RETURN labels(n)"
        ).result_set[0][0].count(SHARED_NODE_LABEL) > 0

    # 1) the relabel fails -> no marker index, node still unlabelled, so a later run WILL retry
    orig_write = writer._write
    monkeypatch.setattr(writer, "_write",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Query timed out")))
    with pytest.raises(RuntimeError):
        writer._ensure_shared_node_index()
    assert not entity_index_present()
    assert not has_entity_label()

    # 2) retry (relabel succeeds) -> node relabelled AND the marker index now exists
    monkeypatch.setattr(writer, "_write", orig_write)
    writer._ensure_shared_node_index()
    assert has_entity_label()
    assert entity_index_present()

    # 3) idempotent: a third run is a no-op (index present -> returns early, no error)
    writer._ensure_shared_node_index()
    assert entity_index_present()


def test_invalidate_embeddings_best_effort_swallows_write_errors(graph, monkeypatch):
    # In a `finally` after a partial write, a secondary invalidation failure must be logged, not
    # raised -- re-raising would mask the original write error (#857 review).
    writer = GraphWriter(graph)
    monkeypatch.setattr(writer, "invalidate_embeddings",
                        lambda items: (_ for _ in ()).throw(RuntimeError("server down")))
    writer._invalidate_embeddings_best_effort([("Function", "x")])  # must NOT raise
