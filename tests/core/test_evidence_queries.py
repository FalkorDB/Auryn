"""Evidence in query answers (agent-evidence design §1-§3): call sites with file:line + code on
who_calls/callees, file paths on locate (incl. the old-graph fallback), compact get_context."""

from __future__ import annotations

import pytest

from auryn.core import graph_queries as gq

_CALLER_SRC = "def caller():\n    target()\n    y = 2\n    target()"


def _seed_call(graph, *, caller_source=True, file_path=True, lines="[11, 13]") -> None:
    caller_props = "id: 'c', name: 'caller', line_start: 10, line_end: 13, signature: 'caller()'"
    if caller_source:
        caller_props += f", source: '{_CALLER_SRC.replace(chr(10), chr(92) + 'n')}'"
    if file_path:
        caller_props += ", file_path: 'pkg/mod.py'"
    edge_props = f"{{resolution: 'resolved', lines: {lines}}}" if lines else "{resolution: 'resolved'}"
    graph.query(
        f"""
        CREATE (c:Function {{{caller_props}}}),
               (t:Function {{id: 't', name: 'target', line_start: 1, line_end: 2, file_path: 'pkg/t.py'}})
        CREATE (c)-[:CALLS {edge_props}]->(t)
        """
    )


class TestCallSites:
    def test_who_calls_returns_file_lines_and_code(self, graph):
        _seed_call(graph)
        edge = gq.callers(graph, "t")["edges"][0]
        assert edge["from"] == "c" and edge["to"] == "t"
        assert edge["lines"] == [11, 13]
        assert edge["file"] == "pkg/mod.py"
        assert edge["sites"] == [{"line": 11, "code": "target()"}, {"line": 13, "code": "target()"}]

    def test_callees_returns_the_same_sites(self, graph):
        _seed_call(graph)
        edge = gq.callees(graph, "c")["edges"][0]
        assert edge["sites"][0] == {"line": 11, "code": "target()"}

    def test_caller_without_source_keeps_lines_but_no_sites(self, graph):
        _seed_call(graph, caller_source=False)
        edge = gq.callers(graph, "t")["edges"][0]
        assert edge["lines"] == [11, 13]
        assert "sites" not in edge

    def test_line_outside_caller_source_is_skipped_not_crashing(self, graph):
        _seed_call(graph, lines="[11, 99, 5]")
        edge = gq.callers(graph, "t")["edges"][0]
        assert [s["line"] for s in edge["sites"]] == [11]

    def test_old_graph_edge_without_lines_still_works(self, graph):
        _seed_call(graph, lines=None)
        edge = gq.callers(graph, "t")["edges"][0]
        assert "lines" not in edge and "sites" not in edge
        assert edge["file"] == "pkg/mod.py"

    def test_at_most_three_sites(self, graph):
        src = "def caller():\\n" + "\\n".join("    target()" for _ in range(6))
        graph.query(
            f"""
            CREATE (c:Function {{id: 'c', name: 'caller', line_start: 1, source: '{src}', file_path: 'm.py'}}),
                   (t:Function {{id: 't', name: 'target'}})
            CREATE (c)-[:CALLS {{lines: [2, 3, 4, 5, 6, 7]}}]->(t)
            """
        )
        edge = gq.callers(graph, "t")["edges"][0]
        assert len(edge["lines"]) == 6
        assert [s["line"] for s in edge["sites"]] == [2, 3, 4]


class TestLocateFile:
    def test_locate_returns_file_and_lines(self, graph):
        _seed_call(graph)
        hit = next(r for r in gq.search_nodes(graph, "Function", "caller") if r["id"] == "c")
        props = hit["properties"]
        assert props["file_path"] == "pkg/mod.py"
        assert props["line_start"] == 10 and props["line_end"] == 13
        assert props["signature"] == "caller()"

    def test_locate_falls_back_to_the_defining_module_file(self, graph):
        graph.query(
            """
            CREATE (m:Module {id: 'm', name: 'mod', file_path: 'pkg/mod.py'}),
                   (k:Class {id: 'k', name: 'Klass'}),
                   (f:Method {id: 'f', name: 'meth'})
            CREATE (m)-[:HAS_CLASS]->(k), (k)-[:HAS_METHOD]->(f)
            """
        )
        hit = gq.search_nodes(graph, "Method", "meth")[0]
        assert hit["properties"]["file_path"] == "pkg/mod.py"
        assert gq.search_nodes(graph, "Class", "Klass")[0]["properties"]["file_path"] == "pkg/mod.py"

    def test_locate_without_any_file_does_not_crash(self, graph):
        graph.query("CREATE (f:Function {id: 'lonely', name: 'lonely'})")
        hit = gq.search_nodes(graph, "Function", "lonely")[0]
        assert "file_path" not in hit["properties"]


def _seed_big(graph, n_lines=100, with_source=True):
    src = "\\n".join(["def big():"] + [f"    x{i} = {i}" for i in range(1, n_lines)])
    source = f", source: '{src}'" if with_source else ""
    graph.query(
        f"""
        CREATE (b:Function {{id: 'big', name: 'big', line_start: 200, line_end: {199 + n_lines},
                             signature: 'big()', doc: '{"d" * 500}', file_path: 'pkg/big.py'{source}}}),
               (c:Function {{id: 'c', name: 'caller', line_start: 10,
                             source: 'def caller():\\n    big()', file_path: 'pkg/c.py'}})
        CREATE (c)-[:CALLS {{lines: [11]}}]->(b)
        """
    )


class TestContextView:
    def test_default_is_compact(self, graph):
        _seed_big(graph)
        view = gq.context_view(graph, "big")
        center = view["center"]
        assert "source" not in center["properties"]
        assert center["properties"]["file_path"] == "pkg/big.py"
        assert len(center["properties"]["doc"]) <= 300
        assert view["source_lines_total"] == 100
        assert len(view["preview"]) == 40
        assert view["preview"][0] == {"line": 200, "code": "def big():"}
        assert view["callers"][0]["sites"] == [{"line": 11, "code": "big()"}]
        assert "nodes" not in view and "edges" not in view  # no duplicate long-id lists
        assert all(n["rel"] != "CALLS" for n in view["neighbors"])  # callers/callees cover calls

    def test_line_range_returns_exact_numbered_lines(self, graph):
        _seed_big(graph)
        view = gq.context_view(graph, "big", lines="250-252")
        assert view["lines"] == [
            {"line": 250, "code": "    x50 = 50"},
            {"line": 251, "code": "    x51 = 51"},
            {"line": 252, "code": "    x52 = 52"},
        ]

    def test_line_range_is_capped(self, graph):
        _seed_big(graph, n_lines=400)
        view = gq.context_view(graph, "big", lines="200-599")
        assert len(view["lines"]) == 200

    def test_line_range_outside_node_is_empty(self, graph):
        _seed_big(graph)
        assert gq.context_view(graph, "big", lines="1-5")["lines"] == []

    @pytest.mark.parametrize("bad", ["bad", "5", "9-3", "-1-4", "a-b"])
    def test_bad_line_range_raises(self, graph, bad):
        _seed_big(graph)
        with pytest.raises(ValueError):
            gq.context_view(graph, "big", lines=bad)

    def test_node_without_source_has_no_preview(self, graph):
        _seed_big(graph, with_source=False)
        view = gq.context_view(graph, "big")
        assert "preview" not in view
        assert view["center"]["properties"]["signature"] == "big()"

    def test_missing_node_is_none(self, graph):
        _seed_big(graph)  # the graph key must exist; only the node is missing
        assert gq.context_view(graph, "nope") is None


class TestCompactSymbols:
    _LONG_SIG = "f(" + ", ".join(f"argument_{i}: dict[str, list[int]] | None = None" for i in range(8)) + ")"

    def _seed(self, graph):
        graph.query(
            f"""CREATE (:Function {{id: 'f', name: 'f', signature: '{self._LONG_SIG}', file_path: 'p/f.py',
                         line_start: 3, line_end: 9, qualified_name: 'p.f', linkage_key: 'extern://python/p::f',
                         decorators: ['x'], unresolved_calls: ['y'], source: 'def f():\\n    pass'}})"""
        )

    def test_locate_keeps_a_long_signature_and_drops_internal_keys(self, graph):
        self._seed(graph)
        props = gq.locate_nodes(graph, "Function", "f")[0]["properties"]
        assert props["signature"].startswith("f(argument_0") and len(props["signature"]) <= 403
        assert props["file_path"] == "p/f.py" and props["qualified_name"] == "p.f"
        for gone in ("linkage_key", "decorators", "unresolved_calls", "source"):
            assert gone not in props

    def test_context_center_keeps_signature(self, graph):
        self._seed(graph)
        assert "signature" in gq.context_view(graph, "f")["center"]["properties"]

    def test_range_outside_the_node_says_so(self, graph):
        self._seed(graph)
        view = gq.context_view(graph, "f", lines="3-10")
        assert [r["line"] for r in view["lines"]] == [3, 4]
        assert "spans lines 3-4" in view["range_note"]

    def test_viewer_search_shape_is_unchanged(self, graph):
        self._seed(graph)
        props = gq.search_nodes(graph, "Function", "f")[0]["properties"]
        assert "linkage_key" in props and "qualified_name" in props  # web viewer keeps the lean bag


def test_context_neighbors_carry_relationship_and_skip_calls(graph):
    graph.query(
        """
        CREATE (m:Module {id: 'm', name: 'mod', file_path: 'pkg/mod.py'}),
               (f:Function {id: 'f', name: 'f', line_start: 1, source: 'def f():\\n    len(x)', file_path: 'pkg/mod.py'}),
               (p:Parameter {id: 'p', name: 'x'}),
               (b:Function {id: 'b', name: 'len', resolved: false})
        CREATE (m)-[:HAS_FUNCTION]->(f), (f)-[:HAS_PARAMETER]->(p), (f)-[:CALLS {lines: [2]}]->(b)
        """
    )
    view = gq.context_view(graph, "f")
    rels = {(n["name"], n["rel"], n["dir"]) for n in view["neighbors"]}
    assert rels == {("mod", "HAS_FUNCTION", "in"), ("x", "HAS_PARAMETER", "out")}
    assert view["callees"][0]["to"] == "b" and view["callees"][0]["sites"] == [{"line": 2, "code": "len(x)"}]


def test_locate_falls_back_through_nested_classes(graph):
    graph.query(
        """
        CREATE (m:Module {id: 'm', name: 'mod', file_path: 'pkg/mod.py'}),
               (o:Class {id: 'o', name: 'Outer'}), (i:Class {id: 'i', name: 'Inner'}),
               (f:Method {id: 'f', name: 'deep'})
        CREATE (m)-[:HAS_CLASS]->(o), (o)-[:HAS_NESTED_CLASS]->(i), (i)-[:HAS_METHOD]->(f)
        """
    )
    assert gq.search_nodes(graph, "Method", "deep")[0]["properties"]["file_path"] == "pkg/mod.py"


def test_context_reports_truncated_call_edges(graph):
    graph.query("CREATE (:Function {id: 'c', name: 'caller', line_start: 1, source: 'def caller():\\n    pass'})")
    for i in range(6):
        graph.query(
            f"MATCH (c:Function {{id: 'c'}}) CREATE (t:Function {{id: 't{i}', name: 't{i}'}}), "
            "(c)-[:CALLS {lines: [2]}]->(t), (c)-[:CALLS {lines: [3]}]->(t)"
        )
    assert gq.context_view(graph, "c")["truncated"] is True


def test_module_caller_sites_come_from_the_edge(graph):
    graph.query(
        "CREATE (m:Module {id: 'm', name: 'mod', file_path: 'pkg/mod.py'}), (t:Function {id: 't', name: 't'}) "
        "CREATE (m)-[:CALLS {lines: [5, 7], site_code: ['5:t()', '7:t(1)']}]->(t)"
    )
    edge = gq.callers(graph, "t")["edges"][0]
    assert edge["sites"] == [{"line": 5, "code": "t()"}, {"line": 7, "code": "t(1)"}]
    assert edge["file"] == "pkg/mod.py" and "_site_code" not in edge


def test_file_path_is_normalised_across_ingestors(graph):
    graph.query(
        "CREATE (:Method {id: 'cs', name: 'Run', file: 'src/App.cs'}), "
        "(f:File {id: 'gf', name: 'a.go', path: 'pkg/a.go'})-[:DEFINES]->(:Function {id: 'go', name: 'Do'})"
    )
    assert gq.search_nodes(graph, "Method", "Run")[0]["properties"]["file_path"] == "src/App.cs"
    assert gq.search_nodes(graph, "Function", "Do")[0]["properties"]["file_path"] == "pkg/a.go"


def test_unnumbered_source_still_gets_a_preview(graph):
    graph.query("CREATE (:Method {id: 'm', name: 'M', source: 'void M() {\\n  x();\\n}'})")
    view = gq.context_view(graph, "m")
    assert view["preview"][0] == {"code": "void M() {"} and "no line numbers" in view["preview_note"]


def test_neighbors_keep_every_relationship_and_survive_many_calls(graph):
    graph.query(
        "CREATE (p:Procedure {id: 'p', name: 'proc'}), (t:Table {id: 't', name: 'orders'}), "
        "(x:Parameter {id: 'x', name: 'arg'}) "
        "CREATE (p)-[:READS]->(t), (p)-[:WRITES]->(t), (p)-[:HAS_PARAMETER]->(x)"
    )
    for i in range(30):
        graph.query(f"MATCH (p {{id: 'p'}}) CREATE (p)-[:CALLS]->(:Function {{id: 'c{i}', name: 'c{i}'}})")
    rels = {(n["name"], n["rel"]) for n in gq.context_view(graph, "p")["neighbors"]}
    assert {("orders", "READS"), ("orders", "WRITES"), ("arg", "HAS_PARAMETER")} <= rels
