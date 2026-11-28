"""Tests for the multi-hop traversal queries added for the MCP tool layer
(impact_of, trace_entrypoint), plus the scale-related fixes to root_nodes
and neighbors (see their docstrings in graph_queries.py for the real-graph
failure these fix). serialize_node/search_nodes/node_detail's basic behavior is
already exercised indirectly via tests/viewer/test_api.py (the Flask app calls
straight into this same module); these tests cover only what's new/changed
here.
"""

from __future__ import annotations

import pytest

from auryn.core import graph_queries as gq


def _seed_call_and_data_graph(graph) -> None:
    """A-calls-B-calls-C chain (B also reachable a second way from A, to
    exercise DISTINCT-dedup), where C writes a table two hops away from A --
    the shape trace_entrypoint/impact_of need to cross correctly."""
    graph.query(
        """
        CREATE (a:Procedure {id: 'a', name: 'TransferEmployee'}),
               (b:Procedure {id: 'b', name: 'LogChange'}),
               (c:Function {id: 'c', name: 'DeepHelper'}),
               (t1:Table {id: 't1', name: 'EMPLOYEES'}),
               (t2:Table {id: 't2', name: 'AUDIT_LOG'})
        CREATE (a)-[:CALLS]->(b), (a)-[:CALLS]->(c), (b)-[:CALLS]->(c)
        CREATE (a)-[:WRITES]->(t1), (c)-[:READS]->(t2)
        """
    )


class TestImpactOf:
    def test_downstream_traversal_follows_default_edge_types(self, graph):
        _seed_call_and_data_graph(graph)

        result = gq.impact_of(graph, "a", direction="downstream", max_depth=3)

        names = {i["node"]["name"] for i in result["impacted"]}
        assert names == {"LogChange", "DeepHelper", "AUDIT_LOG", "EMPLOYEES"}
        assert all(i["direction"] == "downstream" for i in result["impacted"])
        assert result["truncated"] is False

    def test_upstream_traversal_from_a_table(self, graph):
        _seed_call_and_data_graph(graph)

        result = gq.impact_of(graph, "t2", direction="upstream", max_depth=3)

        names = {i["node"]["name"] for i in result["impacted"]}
        assert names == {"TransferEmployee", "LogChange", "DeepHelper"}

    def test_both_directions_reported_separately_not_merged(self, graph):
        _seed_call_and_data_graph(graph)

        result = gq.impact_of(graph, "b", direction="both", max_depth=2)

        by_direction = {i["node"]["name"]: i["direction"] for i in result["impacted"]}
        assert by_direction["DeepHelper"] == "downstream"
        assert by_direction["TransferEmployee"] == "upstream"

    def test_diamond_dependency_deduped_within_one_direction(self, graph):
        # b is reachable from a both directly and via nothing else here, but c is
        # reachable from a both directly (a->c) and via b (a->b->c) -- exactly the
        # diamond shape that would duplicate rows without DISTINCT.
        _seed_call_and_data_graph(graph)

        result = gq.impact_of(graph, "a", direction="downstream", max_depth=3)

        deep_helper_hits = [i for i in result["impacted"] if i["node"]["name"] == "DeepHelper"]
        assert len(deep_helper_hits) == 1

    def test_missing_node_returns_none(self, graph):
        # FalkorDB's RO_QUERY errors against a graph key that's never been
        # written to at all ("Invalid graph operation on empty key") -- the
        # graph needs to exist, just not contain this particular id, to
        # actually exercise the not-found path rather than that unrelated one.
        _seed_call_and_data_graph(graph)
        assert gq.impact_of(graph, "does-not-exist") is None

    def test_rejects_invalid_direction(self, graph):
        _seed_call_and_data_graph(graph)
        with pytest.raises(ValueError, match="direction"):
            gq.impact_of(graph, "a", direction="sideways")

    def test_rejects_out_of_range_max_depth(self, graph):
        _seed_call_and_data_graph(graph)
        with pytest.raises(ValueError, match="max_depth"):
            gq.impact_of(graph, "a", max_depth=999)

    def test_rejects_edge_type_that_isnt_a_plain_identifier(self, graph):
        """The relationship-type allowlist is a Cypher-injection guard, not just
        input validation -- edge_types reaches this function as an MCP tool
        argument, i.e. attacker-influenceable, and rel types can't be bound
        params in Cypher (only values can)."""
        _seed_call_and_data_graph(graph)
        with pytest.raises(ValueError, match="invalid relationship type"):
            gq.impact_of(graph, "a", edge_types=["CALLS] -[r]-() DELETE r //"])

    def test_truncated_flag_set_when_limit_hit(self, graph):
        _seed_call_and_data_graph(graph)

        result = gq.impact_of(graph, "a", direction="downstream", max_depth=3, limit=1)

        assert result["truncated"] is True
        assert len(result["impacted"]) == 1


class TestBehavioralInventory:
    def test_closure_crosses_multiple_call_hops_and_both_table_edges(self, graph):
        _seed_call_and_data_graph(graph)

        result = gq.trace_entrypoint(graph, "a")

        assert {f["name"] for f in result["functions"]} == {"LogChange", "DeepHelper"}
        assert {t["name"] for t in result["tables_written"]} == {"EMPLOYEES"}
        assert {t["name"] for t in result["tables_read"]} == {"AUDIT_LOG"}

    def test_validations_is_explicitly_null_with_a_reason(self, graph):
        _seed_call_and_data_graph(graph)

        result = gq.trace_entrypoint(graph, "a")

        assert result["validations"] is None
        assert "validations_note" in result and result["validations_note"]

    def test_missing_entry_returns_none(self, graph):
        _seed_call_and_data_graph(graph)
        assert gq.trace_entrypoint(graph, "does-not-exist") is None

    def test_max_depth_none_is_unbounded_by_default(self, graph):
        _seed_call_and_data_graph(graph)

        result = gq.trace_entrypoint(graph, "a")

        assert result["max_depth"] is None

    def test_rejects_out_of_range_max_depth(self, graph):
        _seed_call_and_data_graph(graph)
        with pytest.raises(ValueError, match="max_depth"):
            gq.trace_entrypoint(graph, "a", max_depth=0)

    def test_truncated_flag_set_when_function_limit_hit(self, graph):
        _seed_call_and_data_graph(graph)

        result = gq.trace_entrypoint(graph, "a", limit=1)

        assert result["truncated"] is True
        assert len(result["functions"]) == 1


class TestRootNodes:
    def test_finds_the_root_even_when_its_label_isnt_the_smallest(self, graph):
        """root_nodes checks labels smallest-first as a speed heuristic (real
        roots are near-universally small "container" labels) but must still
        find a root that happens to live in the *largest* label -- correctness
        can never depend on that heuristic being right, only its speed in the
        common case."""
        graph.query(
            """
            CREATE (small1:Tiny {id: 't1', name: 'T1'}), (small2:Tiny {id: 't2', name: 'T2'})
            CREATE (small1)-[:REL]->(small2), (small2)-[:REL]->(small1)
            WITH 1 AS x
            UNWIND range(1, 50) AS i
            CREATE (:Big {id: 'b' + toString(i), name: 'B' + toString(i)})
            """
        )
        # Exactly one Big node (b1) is the true root; every other Big node and
        # both Tiny nodes have an incoming edge, so a smallest-first scan has
        # to fall through past the (rootless) Tiny label to find it in Big.
        graph.query(
            """
            MATCH (b1:Big {id: 'b1'})
            UNWIND range(2, 50) AS i
            MATCH (bi:Big {id: 'b' + toString(i)})
            CREATE (b1)-[:REL]->(bi)
            """
        )

        roots = gq.root_nodes(graph, limit=200)

        assert [r["id"] for r in roots] == ["b1"]

    def test_respects_limit_across_labels(self, graph):
        graph.query(
            """
            UNWIND range(1, 5) AS i
            CREATE (:Root {id: 'r' + toString(i), name: 'R' + toString(i)})
            """
        )

        roots = gq.root_nodes(graph, limit=2)

        assert len(roots) == 2


class TestNeighbors:
    def _seed_hub_and_spokes(self, graph, spoke_count: int) -> None:
        graph.query(
            """
            CREATE (hub:Hub {id: 'hub', name: 'Hub'})
            WITH hub
            UNWIND range(1, $n) AS i
            CREATE (hub)-[:REL]->(:Spoke {id: 'spoke' + toString(i), name: 'Spoke' + toString(i)})
            """,
            {"n": spoke_count},
        )

    def test_returns_every_neighbor_when_under_the_limit(self, graph):
        self._seed_hub_and_spokes(graph, 5)

        result = gq.neighbors(graph, "hub", limit=200)

        assert len(result["nodes"]) == 5
        assert result["truncated"] is False

    def test_truncates_a_high_fan_out_node_and_reports_it(self, graph):
        """A node with a huge fan-out (e.g. a real Project with thousands of
        HAS_FILE edges) must never return every neighbor unbounded -- that was
        itself a real, separate scale bug (2s+ for ~195k edges in testing
        against a large-graph shape) on top of root_nodes' timeout."""
        self._seed_hub_and_spokes(graph, 10)

        result = gq.neighbors(graph, "hub", limit=3)

        assert len(result["nodes"]) == 3
        assert result["truncated"] is True

    def test_lean_per_type_cap_bounds_each_type_and_reports_counts(self, graph):
        # A hot File: many CHANGED commits (the new provenance layer) + a few structural DEFINES.
        # get_context's lean + per_type_cap must bound EACH type so the commit flood can't crowd out
        # the structural neighbours, and must report the true fan-out + drop bulky props.
        graph.query("CREATE (f:File {id: 'f', name: 'f'})")
        graph.query("UNWIND range(1, 3) AS j MATCH (f:File {id: 'f'}) "
                    "CREATE (f)-[:DEFINES]->(:Function {id: 'fn' + toString(j), name: 'fn' + toString(j)})")
        graph.query("UNWIND range(1, 30) AS i MATCH (f:File {id: 'f'}) "
                    "CREATE (:GitCommit {id: 'c' + toString(i), sha: toString(i), message: $m})-[:CHANGED]->(f)",
                    {"m": "m" * 300})

        result = gq.neighbors(graph, "f", lean=True, per_type_cap=5)

        changed = [e for e in result["edges"] if e["type"] == "CHANGED"]
        assert len(changed) == 5                                   # the 30 CHANGED are capped at 5
        assert result["edge_counts"]["in"]["CHANGED"] == 30        # true fan-out still reported
        assert result["edge_counts"]["out"]["DEFINES"] == 3
        assert result["truncated"] is True
        assert any(e["type"] == "DEFINES" for e in result["edges"])  # structural neighbours survive the flood
        commits = [n for n in result["nodes"] if n["label"] == "GitCommit"]
        assert commits and all("message" not in n["properties"] for n in commits)  # lean dropped the >200-char message


class TestShortestPath:
    """The single path primitive behind the viewer's Path finder and the MCP
    find_path tool, with an optional edge_types filter (a CALLS-only chain is
    just a parameter). A meaningful directed chain (a->b->c->d) plus a
    disconnected orphan; direction matters, so the same pair resolves
    differently directed vs. undirected."""

    def _seed(self, graph) -> None:
        graph.query(
            """
            CREATE (a:Class {id: 'a', name: 'FastAPI'}),
                   (b:Class {id: 'b', name: 'DefaultPlaceholder'}),
                   (c:Method {id: 'c', name: 'get_request_handler'}),
                   (d:Class {id: 'd', name: 'ModelField'}),
                   (z:Class {id: 'z', name: 'Orphan'})
            CREATE (a)-[:REFERENCES]->(b), (b)-[:CALLS]->(c), (c)-[:REFERENCES]->(d)
            """
        )

    def test_directed_path_follows_the_arrows(self, graph):
        self._seed(graph)

        result = gq.shortest_path(graph, "a", "d", max_depth=6, directed=True)

        assert result["found"] is True
        assert result["hops"] == 3
        assert [n["name"] for n in result["nodes"]] == [
            "FastAPI", "DefaultPlaceholder", "get_request_handler", "ModelField",
        ]
        assert [(e["from"], e["type"], e["to"]) for e in result["edges"]] == [
            ("a", "REFERENCES", "b"),
            ("b", "CALLS", "c"),
            ("c", "REFERENCES", "d"),
        ]

    def test_directed_finds_nothing_against_the_arrows(self, graph):
        self._seed(graph)
        result = gq.shortest_path(graph, "d", "a", directed=True)
        assert result["found"] is False
        assert result["nodes"] == [] and result["edges"] == []

    def test_undirected_ignores_direction(self, graph):
        self._seed(graph)

        result = gq.shortest_path(graph, "d", "a", directed=False)

        assert result["found"] is True
        assert result["hops"] == 3
        assert [n["name"] for n in result["nodes"]] == [
            "ModelField", "get_request_handler", "DefaultPlaceholder", "FastAPI",
        ]
        # even walked backwards, each edge is reported in its real stored direction
        assert {(e["from"], e["type"], e["to"]) for e in result["edges"]} == {
            ("a", "REFERENCES", "b"),
            ("b", "CALLS", "c"),
            ("c", "REFERENCES", "d"),
        }

    def test_no_path_within_depth_bound(self, graph):
        self._seed(graph)
        result = gq.shortest_path(graph, "a", "d", max_depth=2, directed=True)
        assert result["found"] is False

    def test_disconnected_nodes_have_no_undirected_path(self, graph):
        self._seed(graph)
        result = gq.shortest_path(graph, "a", "z", directed=False)
        assert result["found"] is False

    def test_missing_endpoint_returns_none(self, graph):
        self._seed(graph)
        assert gq.shortest_path(graph, "a", "does-not-exist") is None
        assert gq.shortest_path(graph, "does-not-exist", "d") is None

    def test_rejects_out_of_range_max_depth(self, graph):
        self._seed(graph)
        with pytest.raises(ValueError):
            gq.shortest_path(graph, "a", "d", max_depth=0)
        with pytest.raises(ValueError):
            gq.shortest_path(graph, "a", "d", max_depth=gq.MAX_ALLOWED_DEPTH + 1)

    def test_edge_types_restricts_traversal(self, graph):
        self._seed(graph)
        # only b->c is a CALLS edge, so a CALLS-only search can't span a..d...
        assert gq.shortest_path(graph, "a", "d", edge_types=["CALLS"])["found"] is False
        # ...but it does connect the single CALLS hop b->c.
        calls_only = gq.shortest_path(graph, "b", "c", edge_types=["CALLS"])
        assert calls_only["found"] is True
        assert calls_only["edge_types"] == ["CALLS"]

    def test_rejects_edge_type_that_isnt_a_plain_identifier(self, graph):
        self._seed(graph)
        with pytest.raises(ValueError):
            gq.shortest_path(graph, "a", "d", edge_types=["CALLS|DROP"])


class TestDetectCommunities:
    """Label-propagation community detection (FalkorDB algo.labelPropagation).
    Two fully-connected clusters with no edge between them -> LPA converges to
    one community per connected component."""

    def _seed(self, graph) -> None:
        graph.query(
            """
            CREATE (a1:Function {id: 'a1', name: 'a1'}), (a2:Function {id: 'a2', name: 'a2'}),
                   (a3:Function {id: 'a3', name: 'a3'}),
                   (b1:Function {id: 'b1', name: 'b1'}), (b2:Function {id: 'b2', name: 'b2'}),
                   (b3:Function {id: 'b3', name: 'b3'})
            CREATE (a1)-[:CALLS]->(a2), (a2)-[:CALLS]->(a3), (a3)-[:CALLS]->(a1)
            CREATE (b1)-[:CALLS]->(b2), (b2)-[:CALLS]->(b3), (b3)-[:CALLS]->(b1)
            """
        )

    def test_two_clusters_get_two_communities(self, graph):
        self._seed(graph)

        result = gq.detect_communities(graph)

        comm = result["communities"]
        assert result["community_count"] == 2
        # each cluster shares one community id; the two clusters differ
        assert comm["a1"] == comm["a2"] == comm["a3"]
        assert comm["b1"] == comm["b2"] == comm["b3"]
        assert comm["a1"] != comm["b1"]

    def test_ids_scope_the_returned_map(self, graph):
        self._seed(graph)

        result = gq.detect_communities(graph, ids=["a1", "b1"])

        # algorithm still runs graph-wide, but only the requested ids come back
        assert set(result["communities"].keys()) == {"a1", "b1"}

    def test_deterministic_across_runs(self, graph):
        self._seed(graph)
        assert gq.detect_communities(graph)["communities"] == gq.detect_communities(graph)["communities"]


def test_returned_entities_never_include_embedding(graph):
    """The `embedding` vector is stored on nodes for semantic search but must never ride along
    on a returned entity (any serialization path: lean list results + full/raw-cypher nodes)."""
    graph.query(
        "CREATE (:Function {id:'f1', name:'connect', source:'def connect(): ...', "
        "embedding:[0.1, 0.2, 0.3], _fingerprint:'abc'})"
    )
    # lean path (search / who_calls / impact / path lists)
    lean = gq.search_nodes(graph, None, "connect", 10)
    assert lean and lean[0]["properties"].get("name") == "connect"
    assert "embedding" not in lean[0]["properties"]
    assert "_fingerprint" not in lean[0]["properties"]

    # full path (get_context/neighbors center) and raw run_cypher both go through serialize_node
    detail = gq.run_read_query(graph, "MATCH (n:Function {id:'f1'}) RETURN n")
    node = next(n for n in detail["nodes"] if n["id"] == "f1")
    assert node["properties"].get("source")  # real properties are preserved
    assert "embedding" not in node["properties"]
    assert "_fingerprint" not in node["properties"]


def test_graph_schema_does_not_advertise_embedding_key(graph):
    """get_schema's property-key list must omit the internal `embedding` (and `_fingerprint`)
    keys, so an agent isn't even tempted to query the vector."""
    graph.query(
        "CREATE (:Function {id:'f1', name:'connect', source:'...', "
        "embedding:[0.1, 0.2], _fingerprint:'abc'})"
    )
    schema = gq.graph_schema(graph)
    fn_keys = schema["properties_by_label"].get("Function", [])
    assert "name" in fn_keys and "source" in fn_keys  # real keys advertised
    assert "embedding" not in fn_keys
    assert "_fingerprint" not in fn_keys


class TestCallersCalleesEdges:
    """callers()/callees() now return the connecting CALLS edges in the canonical directed shape
    (from -> to = caller -> callee, whichever way we traversed), so the result shows the actual
    call chain, not just a flat node set. Additive to the existing `nodes`/`direction` output."""

    def test_callees_return_directed_call_edges(self, graph):
        _seed_call_and_data_graph(graph)
        res = gq.callees(graph, "a", max_depth=2)
        assert {n["id"] for n in res["nodes"]} == {"b", "c"}
        assert {(e["from"], e["type"], e["to"]) for e in res["edges"]} == {
            ("a", "CALLS", "b"), ("a", "CALLS", "c"), ("b", "CALLS", "c"),
        }

    def test_callers_edges_always_point_caller_to_callee(self, graph):
        _seed_call_and_data_graph(graph)
        res = gq.callers(graph, "c", max_depth=2)
        assert {n["id"] for n in res["nodes"]} == {"a", "b"}
        # traversal was inward (<-CALLS-), but every edge is still emitted caller -> callee
        assert {(e["from"], e["type"], e["to"]) for e in res["edges"]} == {
            ("a", "CALLS", "c"), ("b", "CALLS", "c"), ("a", "CALLS", "b"),
        }

    def test_edge_resolution_provenance_is_surfaced_top_level(self, graph):
        graph.query("CREATE (x:Function {id:'x'})-[:CALLS {resolution:'inferred'}]->(y:Function {id:'y'})")
        edge = gq.callees(graph, "x", max_depth=1)["edges"][0]
        assert (edge["from"], edge["to"], edge["resolution"]) == ("x", "y", "inferred")


class TestCompactTraversalResponses:
    """The traversal tools (who_calls/callees/impact_of/find_path) must NOT echo the full
    `source` of the node the caller already identified by id, nor the full per-edge property
    bag -- that echo, repeated across a multi-call investigation, was the single largest
    avoidable token cost. get_context(id) remains the full-source escape hatch."""

    def test_center_echo_omits_source(self, graph):
        graph.query(
            "CREATE (x:Function {id:'x', name:'charge', source:'def charge(): ...body...'})"
            "-[:CALLS]->(y:Function {id:'y', name:'authorize', source:'def authorize(): ...'})"
        )
        for res in (
            gq.callers(graph, "y"),
            gq.callees(graph, "x"),
            gq.impact_of(graph, "x", direction="downstream"),
        ):
            assert res["center"]["name"]  # still self-describing (id/name/label kept)
            assert "source" not in res["center"]["properties"]
        # get_context (neighbors) is the detail tool -- it DOES keep the center source.
        assert gq.neighbors(graph, "x")["center"]["properties"].get("source")

    def test_find_path_endpoints_omit_source(self, graph):
        graph.query(
            "CREATE (x:Function {id:'x', source:'...body...'})-[:CALLS]->(y:Function {id:'y', source:'...'})"
        )
        res = gq.shortest_path(graph, "x", "y")
        assert res["found"]
        assert "source" not in res["from"]["properties"] and "source" not in res["to"]["properties"]

    def test_traversal_list_nodes_drop_duplication_and_internal_keys(self, graph):
        graph.query(
            "CREATE (a:Function {id:'a', name:'a', qualified_name:'m.a', linkage_key:'extern://a', "
            "signature:'a()', is_async:false, decorators:[], line_start:1})"
            "-[:CALLS]->(b:Function {id:'b', name:'b'})"
        )
        node = gq.callers(graph, "b", max_depth=1)["nodes"][0]
        assert node["id"] == "a" and node["name"] == "a"            # top-level identity preserved
        p = node["properties"]
        assert "id" not in p and "name" not in p                    # the top-level duplication is gone
        assert "linkage_key" not in p and "qualified_name" not in p  # internal / redundant dropped
        assert "is_async" not in p and "decorators" not in p        # empty / low-value dropped
        assert p.get("signature") == "a()" and p.get("line_start") == 1  # locating props survive

    def test_lean_edges_drop_the_full_property_bag(self, graph):
        graph.query(
            "CREATE (x:Function {id:'x'})-[:CALLS {resolution:'resolved', via:'http', "
            "argument_types:['int','str'], type_arguments:['T']}]->(y:Function {id:'y'})"
        )
        edge = gq.callees(graph, "x", max_depth=1)["edges"][0]
        assert (edge["from"], edge["to"], edge["type"]) == ("x", "y", "CALLS")
        assert edge["resolution"] == "resolved" and edge["via"] == "http"
        assert "properties" not in edge  # the full bag (argument_types/type_arguments) is gone


class TestTraceCrossesHttpSeam:
    """trace_entrypoint's closure follows CALLS AND transitively crosses the HTTP seam
    (caller -[:CALLS_API]-> APIEndpoint <-[:EXPOSES_ENDPOINT]- handler), so 'what does this
    endpoint do end to end' spans services/languages instead of stopping at the first repo."""

    def _seed_two_hop_system(self, graph):
        # web/gateway endpoint -> C# controller Method -> service Method -CALLS_API-> platform
        # endpoint <- Python Function handler -> billing.charge -> ledger.record
        graph.query(
            """
            CREATE (e1:APIEndpoint {id:'extern://http/POST:/api/orders', route:'/api/orders',
                                    http_method:'POST', provenance:'http-derived', resolved:true}),
                   (h1:Method   {id:'csharp://gw/ctrl.method.placeorder', name:'PlaceOrder'}),
                   (s1:Method   {id:'csharp://gw/svc.method.checkout',    name:'Checkout'}),
                   (e2:APIEndpoint {id:'extern://http/POST:/charge', route:'/charge',
                                    http_method:'POST', provenance:'http-derived', resolved:true}),
                   (h2:Function {id:'python://pc/module/api.func.charge_endpoint', name:'charge_endpoint'}),
                   (c1:Function {id:'python://pc/module/billing.func.charge', name:'charge'}),
                   (c2:Function {id:'python://pc/module/ledger.func.record', name:'record'})
            CREATE (h1)-[:EXPOSES_ENDPOINT]->(e1),
                   (h1)-[:CALLS]->(s1),
                   (s1)-[:CALLS_API]->(e2),
                   (h2)-[:EXPOSES_ENDPOINT]->(e2),
                   (h2)-[:CALLS]->(c1),
                   (c1)-[:CALLS]->(c2)
            """
        )

    def test_trace_from_endpoint_reaches_across_the_seam(self, graph):
        self._seed_two_hop_system(graph)

        result = gq.trace_entrypoint(graph, "extern://http/POST:/api/orders")

        names = {f["name"] for f in result["functions"]}
        # the gateway handler + service AND, across the HTTP seam, the Python handler and its callees
        assert names == {"PlaceOrder", "Checkout", "charge_endpoint", "charge", "record"}
        assert result["truncated"] is False

    def test_trace_seam_crossing_is_additive_when_no_seam(self, graph):
        # A plain CALLS chain with no CALLS_API behaves exactly as the old single-query closure.
        _seed_call_and_data_graph(graph)
        result = gq.trace_entrypoint(graph, "a")
        assert {f["name"] for f in result["functions"]} == {"LogChange", "DeepHelper"}


class TestTraceOrmMappedTables:
    """trace_entrypoint follows the ORM stitch's Class -MAPS_TO-> Table from the models in its
    closure into `tables_mapped`. Regression: ORM-backed code used to report no tables at all,
    because the trace collected them from READS/WRITES edges only."""

    def _seed_orm_service(self, graph):
        # place_order constructs Order (no __init__: CALLS -> the Class) and Payment (its own
        # __init__: CALLS -> the Method); Customer maps to a table but nothing in the closure uses it.
        graph.query(
            """
            CREATE (f:Function {id:'python://app/module/service.func.place_order', name:'place_order'}),
                   (o:Class    {id:'python://app/module/models.class.order', name:'Order'}),
                   (p:Class    {id:'python://app/module/models.class.payment', name:'Payment'}),
                   (pinit:Method {id:'python://app/module/models.class.payment.method.__init__', name:'__init__'}),
                   (c:Class    {id:'python://app/module/models.class.customer', name:'Customer'}),
                   (t1:Table  {id:'sql://db/table/orders', name:'orders'}),
                   (k1:Column {id:'sql://db/table/orders/column/id', name:'id'}),
                   (t2:Table  {id:'sql://db/table/payments', name:'payments'}),
                   (t3:Table  {id:'sql://db/table/customers', name:'customers'})
            CREATE (f)-[:CALLS]->(o), (f)-[:CALLS]->(pinit), (p)-[:HAS_METHOD]->(pinit),
                   (o)-[:MAPS_TO]->(t1), (o)-[:MAPS_TO]->(k1), (p)-[:MAPS_TO]->(t2), (c)-[:MAPS_TO]->(t3)
            """
        )

    def test_models_in_the_closure_report_their_tables(self, graph):
        self._seed_orm_service(graph)

        result = gq.trace_entrypoint(graph, "python://app/module/service.func.place_order")

        # Order through the Class, Payment through its __init__; not the unused Customer's table,
        # and never a mapped Column.
        assert {t["name"] for t in result["tables_mapped"]} == {"orders", "payments"}
        # MAPS_TO carries no read/write direction, so neither directed list guesses one.
        assert result["tables_read"] == [] and result["tables_written"] == []
        assert result["truncated"] is False

    def test_truncated_flag_set_when_mapped_limit_hit(self, graph):
        # One model mapped to the same table name in two schemas: one closure node, two tables.
        graph.query(
            """
            CREATE (f:Function {id:'f', name:'save'}), (o:Class {id:'o', name:'Order'}),
                   (t1:Table {id:'oracle://db.ORDERS', name:'ORDERS'}),
                   (t2:Table {id:'postgresql://db.orders', name:'orders'})
            CREATE (f)-[:CALLS]->(o), (o)-[:MAPS_TO]->(t1), (o)-[:MAPS_TO]->(t2)
            """
        )

        result = gq.trace_entrypoint(graph, "f", limit=1)

        assert result["truncated"] is True
        assert len(result["tables_mapped"]) == 1


class TestRunReadQueryTabular:
    """run_read_query returns `columns`/`rows` for result data that isn't a graph entity
    (projected properties, aggregations), while still returning graph nodes/edges for the canvas."""

    def _seed(self, graph):
        graph.query(
            "CREATE (:Function {id:'f1', name:'charge', origin:'project'}), "
            "(:Function {id:'f2', name:'eval', origin:'builtin'})"
        )

    def test_scalar_projection_returns_rows_keyed_by_column(self, graph):
        self._seed(graph)
        res = gq.run_read_query(graph, "MATCH (n:Function) RETURN n.name AS name, n.origin AS origin ORDER BY name")
        assert res["columns"] == ["name", "origin"]
        assert res["rows"] == [
            {"name": "charge", "origin": "project"},
            {"name": "eval", "origin": "builtin"},
        ]
        assert res["row_count"] == 2

    def test_aggregation_returns_rows(self, graph):
        self._seed(graph)
        res = gq.run_read_query(graph, "MATCH (n:Function) RETURN count(n) AS c")
        assert res["rows"] == [{"c": 2}]

    def test_nodes_and_scalars_coexist(self, graph):
        self._seed(graph)
        res = gq.run_read_query(graph, "MATCH (n:Function {id:'f1'}) RETURN n, n.name AS name")
        # backward-compat: the graph node is still harvested for the canvas view
        assert any(nd["id"] == "f1" for nd in res["nodes"])
        # tabular view carries both the (compacted) node and the scalar, keyed by column
        assert res["rows"][0]["name"] == "charge"
        assert res["rows"][0]["n"]["id"] == "f1"
