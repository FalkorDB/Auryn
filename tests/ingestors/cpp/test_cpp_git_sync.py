"""Integration tests for C++ ingestion wiring, against the real `graph` fixture. Proves the
ingestor is registered as the owner of C++ files (`.cpp`/`.hpp`/...) in the multi-language
endpoint and that the fast tree-sitter engine models the demo sample end-to-end: the namespace
tier, class/struct/enum extraction (incl. `enum class`), EXTENDS (no IMPLEMENTS), out-of-line
`Owner::method` definitions merged back into their class, template types, C-family macros and
project-internal #includes, and call resolution (receiver-typed obj./ptr->, `Class::static`,
`new` -> constructor, bare -> free function). Needs FalkorDB (the conftest fixture)."""

from __future__ import annotations

import shutil
from pathlib import Path

import git

from auryn.core import ingest
from auryn.core.graph_client import GraphWriter

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SAMPLE = _REPO_ROOT / "demo" / "cpp-ingestor" / "sample"


def _ingest(graph):
    ingest.ingest_directory(GraphWriter(graph), str(_SAMPLE), project_name="shapes")

    def q(cypher):
        return graph.query(cypher).result_set

    return q


def test_ingest_directory_owns_cpp(graph):
    report = ingest.ingest_directory(GraphWriter(graph), str(_SAMPLE), project_name="shapes")

    ingested = {r.language: r for r in report.languages if r.result is not None}
    assert "cpp" in ingested
    assert ingested["cpp"].files_detected == 3  # geometry.hpp + shapes.cpp + main.cpp
    assert not any(u["language"] == "cpp" for u in report.unsupported_detected)


def test_namespace_tier_and_types(graph):
    q = _ingest(graph)
    assert q("MATCH (:Project)-[:HAS_NAMESPACE]->(n:Namespace) RETURN n.name") == [["geo"]]
    # geo contains the Enum, the Struct and the four Classes (Shape, Circle, Box, Scene).
    assert q("MATCH (:Namespace {name:'geo'})-[:HAS_TYPE]->(t) RETURN count(t)")[0][0] == 6
    assert q("MATCH (e:Enum {name:'ShapeKind'}) RETURN e.is_scoped") == [[True]]  # `enum class`
    assert q("MATCH (:Enum {name:'ShapeKind'})-[:HAS_ENUM_MEMBER]->(m) RETURN count(m)")[0][0] == 2
    assert q("MATCH (:Struct {name:'Point'})-[:HAS_FIELD]->(f) RETURN count(f)")[0][0] == 2


def test_template_type_captured(graph):
    q = _ingest(graph)
    assert q("MATCH (c:Class {name:'Box'}) RETURN c.is_template, c.type_parameters") == [[True, ["T"]]]


def test_extends_no_implements(graph):
    q = _ingest(graph)
    assert q("MATCH (:Class {name:'Circle'})-[:EXTENDS]->(c:Class {name:'Shape'}) RETURN 1") == [[1]]
    # C++ has no interfaces: the ingestor never writes an IMPLEMENTS edge.
    assert q("MATCH ()-[r:IMPLEMENTS]->() RETURN count(r)") == [[0]]


def test_default_arg_method_decl_and_def_merge(graph, tmp_path):
    """A method whose DECLARATION carries a default argument and whose out-of-line DEFINITION does not
    must merge into ONE node: the parser keeps the defaulted param and excises its default from the type,
    so decl and def share an id. Before the fix the decl dropped the param -> two nodes (and the decl-only
    one went stale on resync; nlohmann/Catch2 finding)."""
    d = tmp_path / "defarg"
    d.mkdir()
    (d / "w.hpp").write_text(
        "#pragma once\nnamespace n {\nclass W {\n public:\n  int eq(int x, int cs = 1);\n};\n}\n")
    (d / "w.cpp").write_text('#include "w.hpp"\nnamespace n {\nint W::eq(int x, int cs) { return x + cs; }\n}\n')
    ingest.ingest_directory(GraphWriter(graph), str(d), project_name="defarg")

    # ONE merged, defined method (not a stale decl-only twin), and the defaulted param is captured.
    assert graph.query(
        "MATCH (:Class {name:'W'})-[:HAS_METHOD]->(m:Method {name:'eq'}) RETURN count(m), m.defined"
    ).result_set == [[1, True]]
    assert graph.query(
        "MATCH (:Class {name:'W'})-[:HAS_METHOD]->(:Method {name:'eq'})-[:HAS_PARAMETER]->(p) RETURN count(p)"
    ).result_set[0][0] == 2


def test_out_of_line_definition_merges_into_class(graph):
    q = _ingest(graph)
    # `double geo::Circle::area() const {..}` (in shapes.cpp) merges with the in-class declaration
    # (in geometry.hpp) into ONE Method node on Circle, marked defined.
    assert q("MATCH (:Class {name:'Circle'})-[:HAS_METHOD]->(m:Method {name:'area'}) RETURN count(m), m.defined") == [[1, True]]
    # The constructor, declared in the header and defined out-of-line, is likewise one Method.
    assert q(
        "MATCH (:Class {name:'Circle'})-[:HAS_METHOD]->(m:Method {name:'Circle', kind:'CONSTRUCTOR'}) "
        "RETURN count(m), m.defined"
    ) == [[1, True]]


def test_free_functions(graph):
    q = _ingest(graph)
    assert q("MATCH (:Namespace {name:'geo'})-[:HAS_FUNCTION]->(f:Function {name:'make_point'}) RETURN 1") == [[1]]
    assert q("MATCH (:Project)-[:HAS_FUNCTION]->(f:Function {name:'main'}) RETURN 1") == [[1]]


def test_call_resolution(graph):
    q = _ingest(graph)
    # receiver-typed obj./ptr->: Scene.totalArea calls area() on shape_ (field: Circle) / other (Circle*).
    assert q("MATCH (:Method {name:'totalArea'})-[:CALLS]->(m:Method {name:'area'}) RETURN 1") == [[1]]
    # Class::static(): Scene::limit()
    assert q("MATCH (:Method {name:'totalArea'})-[:CALLS]->(m:Method {name:'limit'}) RETURN 1") == [[1]]
    # new Circle(...) -> Circle's constructor
    assert q(
        "MATCH (:Method {name:'totalArea'})-[:CALLS]->(m:Method {name:'Circle', kind:'CONSTRUCTOR'}) RETURN 1"
    ) == [[1]]
    # bare call -> free function
    assert q("MATCH (:Method {name:'totalArea'})-[:CALLS]->(f:Function {name:'make_point'}) RETURN 1") == [[1]]


def test_depends_on(graph):
    q = _ingest(graph)
    # Scene::totalArea takes a Circle* and Scene::shape_ is a Circle -> DEPENDS_ON Circle.
    targets = {r[0] for r in q("MATCH (:Method {name:'totalArea'})-[:DEPENDS_ON]->(t) RETURN t.name")}
    assert "Circle" in targets


def test_macros_and_includes(graph):
    q = _ingest(graph)
    assert q("MATCH (:File {name:'geometry.hpp'})-[:DEFINES]->(m:Macro {name:'MAX_SIDES'}) RETURN 1") == [[1]]
    assert sorted(r[0] for r in q("MATCH (a:File)-[:INCLUDES]->(:File {name:'geometry.hpp'}) RETURN a.name")) == ["main.cpp", "shapes.cpp"]
    # <string>/<cstdio> are system headers outside the project -> no File node, no INCLUDES edge.
    assert q("MATCH (:File)-[:INCLUDES]->(:File {name:'string'}) RETURN 1") == []


def test_resync_same_content_is_a_noop(graph):
    ingest.ingest_directory(GraphWriter(graph), str(_SAMPLE), project_name="shapes")
    report = ingest.ingest_directory(GraphWriter(graph), str(_SAMPLE), project_name="shapes")
    r = next(x.result for x in report.languages if x.language == "cpp")
    assert (r.nodes_added, r.nodes_removed, r.edges_added, r.edges_removed) == (0, 0, 0, 0)


def test_ingest_repository_owns_cpp(graph, tmp_path):
    origin = tmp_path / "shapes_app"
    shutil.copytree(_SAMPLE, origin)
    repo = git.Repo.init(str(origin), initial_branch="main")
    with repo.config_writer() as cw:
        cw.set_value("user", "email", "t@e.com")
        cw.set_value("user", "name", "T")
    repo.git.add(A=True)
    commit = repo.index.commit("init").hexsha

    report = ingest.ingest_repository(GraphWriter(graph), str(origin), ref=commit, project_name="shapes")

    assert "cpp" in [r.language for r in report.languages if r.result is not None]
    assert graph.query(
        "MATCH (:Class {name:'Circle'})-[:EXTENDS]->(:Class {name:'Shape'}) RETURN 1"
    ).result_set == [[1]]


def test_doth_header_routed_to_cpp_by_content(graph, tmp_path):
    """`.h` is C's by default, but a `.h` with C++ content must go to the C++ ingestor (real C++ uses
    `.h` for headers, e.g. protobuf). A class declared in a `.h` must be captured as a `cpp://` Class
    (not mangled by the C grammar) and join its `.cc` definition; a sibling pure-C `.h`+`.c` stays C."""
    d = tmp_path / "mixed"
    d.mkdir()
    (d / "widget.h").write_text(
        "#pragma once\nnamespace acme {\nclass Widget {\n public:\n  Widget(int id);\n  int Compute(int x);\n};\n}\n"
    )
    (d / "widget.cc").write_text(
        '#include "widget.h"\nnamespace acme {\nWidget::Widget(int id) {}\nint Widget::Compute(int x) { return x; }\n}\n'
    )
    (d / "clib.h").write_text("struct Point { int x; int y; };\nint add(int a, int b);\n")
    (d / "clib.c").write_text('#include "clib.h"\nint add(int a, int b) { return a + b; }\n')

    ingest.ingest_directory(GraphWriter(graph), str(d), project_name="mixed")

    def q(c):
        return graph.query(c).result_set

    # The C++ class from widget.h is captured with its methods -- and NOT mangled into a c:// node.
    assert q("MATCH (c:Class {name:'Widget'}) RETURN c.id") == [["cpp://mixed/namespace/acme.class.widget"]]
    assert q("MATCH (:Class {name:'Widget'})-[:HAS_METHOD]->(m) RETURN count(m)")[0][0] == 2
    assert q("MATCH (n) WHERE n.id STARTS WITH 'c://' AND n.id CONTAINS 'idget' RETURN count(n)") == [[0]]
    # The pure-C header stays with the C ingestor (c:// struct), paired with clib.c.
    assert q("MATCH (s:Struct {name:'Point'}) RETURN s.id") == [["c://mixed/struct/Point"]]


def test_header_language_flip_cleans_up(graph, falkordb_connection, tmp_path):
    """A `.h` whose content flips C<->C++ across commits must leave the incremental graph identical to a
    full recompute -- the old owner's nodes (incl. its Project) are removed, the new owner's created.
    This is the sole-file case (the dir's only file flips), where the old language drops to zero files."""
    from uuid import uuid4

    origin = tmp_path / "flip"
    origin.mkdir()
    repo = git.Repo.init(str(origin), initial_branch="main")
    with repo.config_writer() as cw:
        cw.set_value("user", "email", "t@e.com")
        cw.set_value("user", "name", "T")
    hdr = origin / "only.h"
    hdr.write_text("struct Point { int x; int y; };\n")            # commit A: pure C
    repo.git.add(A=True)
    sha_a = repo.index.commit("A: C header").hexsha
    hdr.write_text("namespace n { class Foo { public: int m(); }; }\n")  # commit B: C++
    repo.git.add(A=True)
    sha_b = repo.index.commit("B: C++ header").hexsha
    cache = tmp_path / "cache"

    # incremental: ingest A, then resync to B on the SAME graph.
    ingest.ingest_repository(GraphWriter(graph), str(origin), ref=sha_a, project_name="flip", cache_root=cache)
    assert graph.query("MATCH (n) WHERE n.id STARTS WITH 'c://' RETURN count(n)").result_set[0][0] > 0  # C at A
    ingest.ingest_repository(GraphWriter(graph), str(origin), ref=sha_b, project_name="flip", cache_root=cache)

    # full recompute of B into a fresh graph.
    full = falkordb_connection.select_graph(f"flipfull_{uuid4().hex}")
    try:
        ingest.ingest_repository(GraphWriter(full), str(origin), ref=sha_b, project_name="flip", cache_root=cache)
        inc_ids = {r[0] for r in graph.query("MATCH (n) RETURN n.id").result_set}
        full_ids = {r[0] for r in full.query("MATCH (n) RETURN n.id").result_set}
        assert inc_ids == full_ids, f"incremental != full after flip; only-in-incremental: {sorted(inc_ids - full_ids)}"
        # and the flip actually happened: C++ class present, no stale C nodes.
        assert graph.query("MATCH (c:Class {name:'Foo'}) RETURN count(c)").result_set[0][0] == 1
        assert graph.query("MATCH (n) WHERE n.id STARTS WITH 'c://' RETURN count(n)").result_set[0][0] == 0
    finally:
        full.delete()


def test_ambiguous_include_basename_split_across_seed_and_emit(graph, falkordb_connection, tmp_path):
    """Two headers share a basename in different dirs (`a/shared.hpp`, `b/shared.hpp`), so an
    `#include "shared.hpp"` is ambiguous and the FULL path resolves no INCLUDES edge. On an incremental
    resync where one header is unchanged (seed) and the other + the includer changed (emit), the basename
    must still be judged ambiguous over the seed+emit UNION -- not unique within each half -- else the
    incremental wrongly resolves the include and diverges from a full recompute (nlohmann/json repro)."""
    from uuid import uuid4

    origin = tmp_path / "amb"
    origin.mkdir()
    repo = git.Repo.init(str(origin), initial_branch="main")
    with repo.config_writer() as cw:
        cw.set_value("user", "email", "t@e.com")
        cw.set_value("user", "name", "T")
    (origin / "a").mkdir()
    (origin / "b").mkdir()
    (origin / "src").mkdir()
    (origin / "a" / "shared.hpp").write_text("namespace a { struct S { int x; }; }\n")
    (origin / "b" / "shared.hpp").write_text("namespace b { struct T { int y; }; }\n")
    (origin / "src" / "use.cpp").write_text('#include "shared.hpp"\nint use() { return 0; }\n')
    repo.git.add(A=True)
    sha_a = repo.index.commit("A").hexsha
    # commit B: change b/shared.hpp (-> emit) and the includer use.cpp (-> emit); a/shared.hpp stays (seed)
    (origin / "b" / "shared.hpp").write_text("namespace b { struct T { int y; int z; }; }\n")
    (origin / "src" / "use.cpp").write_text('#include "shared.hpp"\nint use() { return 1; }  // changed\n')
    repo.git.add(A=True)
    sha_b = repo.index.commit("B").hexsha
    cache = tmp_path / "cache"

    # incremental: ingest A, resync to B on the same graph.
    ingest.ingest_repository(GraphWriter(graph), str(origin), ref=sha_a, project_name="amb", cache_root=cache)
    ingest.ingest_repository(GraphWriter(graph), str(origin), ref=sha_b, project_name="amb", cache_root=cache)

    full = falkordb_connection.select_graph(f"ambfull_{uuid4().hex}")
    try:
        ingest.ingest_repository(GraphWriter(full), str(origin), ref=sha_b, project_name="amb", cache_root=cache)

        def snap(g):
            nodes = {r[0] for r in g.query("MATCH (n) RETURN n.id").result_set}
            edges = {(r[0], r[1], r[2]) for r in g.query("MATCH (a)-[r]->(b) RETURN a.id, type(r), b.id").result_set}
            return nodes, edges

        inc_n, inc_e = snap(graph)
        full_n, full_e = snap(full)
        assert inc_n == full_n, f"nodes diverged: {sorted(inc_n ^ full_n)}"
        assert inc_e == full_e, f"edges diverged: only-in-incremental {sorted(inc_e - full_e)}"
        # the ambiguous include resolves to NO file (the full path's behavior) in both graphs.
        assert graph.query("MATCH (:File)-[r:INCLUDES]->() RETURN count(r)").result_set[0][0] == 0
    finally:
        full.delete()
