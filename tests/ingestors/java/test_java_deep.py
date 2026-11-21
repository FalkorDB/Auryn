"""Java deep pass (scip-java / SCIP) -- the `deep_sync` engine mirroring Python/C#/Rust/TS/Go.

Most tests are pure and need no toolchain: they drive the shared SCIP reader/resolver with a committed
`index.scip` captured from a real `scip-java index` run (fixtures/jprobe.scip + its Maven source project),
so CI exercises the resolution without scip-java / a JDK / a build tool installed. The end-to-end ingest
test is gated on scip-java actually being available (and, since scip-java RUNS THE BUILD, on the project's
build tool + JDK)."""

from __future__ import annotations

import glob
import os
import subprocess
from pathlib import Path

import pytest
from auryn.core import ingest, scip
from auryn.core.graph_client import GraphWriter
from auryn.core.scip_resolve import _is_callable_symbol, _simple_name, resolve_calls
from auryn.ingestors.java import resolution, scip_java
from auryn.ingestors.java.file_parser import parse_source
from auryn.ingestors.java.ids import project_id

_FIX = Path(__file__).parent / "fixtures"
_SCIP = _FIX / "jprobe.scip"
_SRC = _FIX / "jprobe_src"
_PROJ = project_id("jprobe")


def _fixture_files():
    return [parse_source(Path(p).read_bytes(), os.path.relpath(p, _SRC))
            for p in sorted(glob.glob(str(_SRC / "src/main/java/**/*.java"), recursive=True))]


def _fixture_locations():
    return resolution.callable_locations(_fixture_files(), _PROJ)


# -- SCIP reader --------------------------------------------------------------------------------

def test_scip_reader_decodes_occurrences():
    docs = scip.parse_index(_SCIP.read_bytes())
    assert {d.relative_path for d in docs} >= {"src/main/java/com/example/model/Store.java"}
    assert sum(len(d.occurrences) for d in docs) > 0
    store = next(d for d in docs if d.relative_path.endswith("Store.java"))
    assert any(o.is_definition and o.symbol.endswith("size().") for o in store.occurrences)


def test_symbol_helpers_on_java_symbols():
    # scip-java's symbol scheme (0.12.x emits `semanticdb`, 0.14.x `scip-java`); the descriptor grammar is
    # the same -- callables end `).`, overloads carry `(+N)`, constructors are backtick-wrapped `<init>`.
    pre = "semanticdb maven maven/com.example/jprobe 1.0 "
    assert _is_callable_symbol(pre + "com/example/app/Service#save().")
    assert _is_callable_symbol(pre + "com/example/model/Store#`<init>`().")
    assert not _is_callable_symbol(pre + "com/example/model/Store#buf.")   # a field (term), not a call
    assert _simple_name(pre + "com/example/app/Service#save().") == "save"
    assert _simple_name(pre + "com/example/model/Store#`<init>`().") == "<init>"
    # a scip-java 0.14.x overload disambiguator still yields the bare name
    assert _simple_name("scip-java maven . . com/example/model/Store#put(+1).") == "put"


# -- callable_locations parity -------------------------------------------------------------------

def test_callable_locations_match_resolution_node_ids():
    files = _fixture_files()
    locs = resolution.callable_locations(files, _PROJ)
    node_ids = {i.key["id"] for i in resolution.resolve(files, _PROJ)
                if type(i).__name__ == "GraphNode" and i.label == "Method"}
    assert {loc.id for loc in locs} <= node_ids
    # (the fixture classes have no DECLARED constructors, so no `<init>` locations -- the ctor->`<init>`
    # name mapping is exercised by callable_locations' code, not asserted here)
    assert {loc.name for loc in locs} >= {"save", "saveMany", "put", "record", "size", "count"}


# -- the resolver: PRECISE OVERLOAD SELECTION + cross-package dispatch ----------------------------

def test_resolve_calls_selects_the_right_overload():
    locs = _fixture_locations()
    deep = resolve_calls(_SCIP.read_bytes(), locs, require_call_site=True, emit_self_calls=True)
    by_id = {loc.id: loc for loc in locs}
    for e in deep.edges:
        assert e.properties["resolution"] == "resolved"
    pairs = {(by_id[e.from_key["id"]].name, e.to_key["id"]) for e in deep.edges}

    def to_id(caller, contains):
        return any(name == caller and contains in tid for name, tid in pairs)

    # THE headline Java win: `store.put(v)` vs `store.put(v, n)` resolve to the DIFFERENT overloads -- the
    # name pass sees both as "put" and can't choose.
    assert to_id("save", ".method.put(string)")
    assert to_id("saveMany", ".method.put(string,int)")
    assert not to_id("save", ".method.put(string,int)")
    # cross-package / receiver-typed dispatch and an intra-class call.
    assert to_id("run", ".method.save(")
    assert to_id("count", ".method.size(")


def test_constructor_new_calls_are_recorded_as_init():
    # `new Thing(1)` is parsed under the type name `Thing`, but scip-java reports the constructor reference
    # as `Thing#<init>().`; callable_locations must normalize it to `<init>` in the caller's called_names so
    # the syntactic-call gate accepts the resolved constructor edge (rather than drop the fast one).
    src = {
        "src/main/java/com/example/model/Thing.java":
            "package com.example.model;\npublic class Thing { public Thing(int n) {} }\n",
        "src/main/java/com/example/app/Factory.java":
            "package com.example.app;\nimport com.example.model.Thing;\n"
            "public class Factory { public Thing make() { return new Thing(1); } }\n",
    }
    raw = [parse_source(s.encode(), p) for p, s in src.items()]
    locs = resolution.callable_locations(raw, _PROJ)
    make = next(loc for loc in locs if loc.name == "make")
    assert "<init>" in make.called_names and "Thing" not in make.called_names
    # the normalization also carries to called_sites (with the `new Thing(1)` line), so the line-precise
    # gate matches scip-java's `Thing#<init>().` occurrence on that line.
    assert ("<init>", 3) in make.called_sites and not any(n == "Thing" for n, _ in make.called_sites)


def test_call_sites_record_callee_identifier_lines_including_repeats():
    # called_sites feeds the line-precise gate: it records each call at its callee-identifier line, and
    # keeps EVERY line of a repeated call (a name-only dedup would drop one).
    src = {
        "src/main/java/com/example/A.java":
            "package com.example;\n"      # line 1
            "public class A {\n"          # line 2
            "  void caller(B b) {\n"      # line 3
            "    b.run();\n"              # line 4
            "    b.run();\n"              # line 5 -- repeated call on another line
            "    helper();\n"            # line 6
            "  }\n"
            "  void helper() {}\n"
            "}\n",
    }
    raw = [parse_source(s.encode(), p) for p, s in src.items()]
    caller = next(loc for loc in resolution.callable_locations(raw, _PROJ) if loc.name == "caller")
    assert {("run", 4), ("run", 5), ("helper", 6)} <= caller.called_sites


def test_resolve_calls_tracks_coverage():
    locs = _fixture_locations()
    deep = resolve_calls(_SCIP.read_bytes(), locs, require_call_site=True, emit_self_calls=True)
    by_name = {loc.name: loc for loc in locs}
    assert {by_name[n].id for n in ("save", "saveMany", "count")} <= deep.covered_ids
    assert deep.metrics["resolved"] >= 5


def test_resolve_calls_is_empty_without_matching_locations():
    deep = resolve_calls(_SCIP.read_bytes(), [])
    assert deep.edges == [] and deep.metrics["resolved"] == 0 and deep.covered_ids == set()


# -- graceful fallback ---------------------------------------------------------------------------

def _depth(graph, name):
    r = graph.ro_query("MATCH (p:Project {name:$n}) RETURN p.depth", {"n": name}).result_set
    return r[0][0] if r else None


def test_deep_falls_back_to_treesitter_when_unavailable(graph, monkeypatch):
    def _boom(_root):
        raise scip_java.ScipJavaUnavailable("forced: no scip-java in this test")

    monkeypatch.setattr(scip_java, "run_scip", _boom)
    report = ingest.ingest_directory(GraphWriter(graph), str(_SRC), project_name="jfb", deep="inline")

    java = next(r for r in report.languages if r.language == "java")
    assert java.error is None
    assert java.deep_status != "error"  # ScipJavaUnavailable is HANDLED, not an escaped error
    assert _depth(graph, "jfb") == "fast"


def test_fast_rerun_does_not_downgrade_a_deep_graph(graph):
    ingest.ingest_directory(GraphWriter(graph), str(_SRC), project_name="jdg", deep="off")
    assert _depth(graph, "jdg") == "fast"
    graph.query("MATCH (p:Project {name:'jdg'}) SET p.depth='deep'")
    ingest.ingest_directory(GraphWriter(graph), str(_SRC), project_name="jdg", deep="off")
    assert _depth(graph, "jdg") == "deep"


# -- end-to-end with a real scip-java (gated on availability) ------------------------------------

def _scip_java_available() -> bool:
    try:
        cmd = scip_java._tool_command()
    except scip_java.ScipJavaUnavailable:
        return False
    try:
        return subprocess.run([*cmd, "--version"], capture_output=True, timeout=180, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.mark.skipif(not _scip_java_available(),
                    reason="requires a working scip-java + the project's build tool (Maven/Gradle) + JDK 17+")
def test_deep_inline_resolves_overloads_end_to_end(graph, tmp_path):
    import shutil

    proj = tmp_path / "jprobe"
    shutil.copytree(_SRC, proj)
    report = ingest.ingest_directory(GraphWriter(graph), str(proj), project_name="je", deep="inline")

    java = next(r for r in report.languages if r.language == "java")
    assert java.deep_status != "error"  # an escaped deep error is a real failure, not an unavailable build
    if _depth(graph, "je") != "deep":  # scip-java present but the build couldn't run here -> skip, don't fail
        pytest.skip(f"scip-java did not produce a deep graph (deep_status={java.deep_status}); build unavailable")

    # the overloads resolve to the right targets end-to-end.
    one = graph.query(
        "MATCH (:Method {name:'save'})-[:CALLS {resolution:'resolved'}]->(m:Method {name:'put'}) "
        "WHERE m.id CONTAINS '.method.put(string)' RETURN 1"
    ).result_set
    two = graph.query(
        "MATCH (:Method {name:'saveMany'})-[:CALLS {resolution:'resolved'}]->(m:Method {name:'put'}) "
        "WHERE m.id CONTAINS '.method.put(string,int)' RETURN 1"
    ).result_set
    assert one == [[1]] and two == [[1]]
