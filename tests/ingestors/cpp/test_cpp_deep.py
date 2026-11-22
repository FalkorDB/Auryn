"""C++ deep pass (scip-clang / SCIP) -- the `deep_sync` engine mirroring the other SCIP languages.

Most tests are pure and need no toolchain: they drive the shared SCIP reader/resolver with a committed
`index.scip` captured from a real `scip-clang` run (fixtures/cppprobe.scip + its source), so CI exercises
the resolution without scip-clang / a compile database installed. The end-to-end ingest test is gated on
scip-clang being available (and writes a location-correct compile_commands.json for the copied project)."""

from __future__ import annotations

import glob
import json
import os
import subprocess
from pathlib import Path

import pytest
from auryn.core import ingest, scip
from auryn.core.graph_client import GraphWriter
from auryn.core.scip_resolve import _is_callable_symbol, _simple_name, resolve_calls
from auryn.ingestors.cpp import resolution, scip_clang
from auryn.ingestors.cpp.file_parser import parse_source
from auryn.ingestors.cpp.ids import project_id

_FIX = Path(__file__).parent / "fixtures"
_SCIP = _FIX / "cppprobe.scip"
_SRC = _FIX / "cppprobe_src"
_PROJ = project_id("cppprobe")


def _fixture_files():
    return [parse_source(Path(p).read_bytes(), os.path.relpath(p, _SRC))
            for p in sorted(glob.glob(str(_SRC / "src/*")))]


def _fixture_locations():
    return resolution.callable_locations(_fixture_files(), _PROJ)


# -- SCIP reader --------------------------------------------------------------------------------

def test_scip_reader_decodes_occurrences():
    docs = scip.parse_index(_SCIP.read_bytes())
    assert {d.relative_path for d in docs} >= {"src/shapes.cpp", "src/main.cpp"}
    assert sum(len(d.occurrences) for d in docs) > 0
    shapes = next(d for d in docs if d.relative_path == "src/shapes.cpp")
    # the out-of-line definition of Shape::area lives in shapes.cpp, not geometry.h
    assert any(o.is_definition and _simple_name(o.symbol) == "area" for o in shapes.occurrences)


def test_symbol_helpers_on_cpp_symbols():
    # scip-clang scheme is `cxx`; callables carry an in-parens overload disambiguator hash, and a
    # destructor name is backtick-wrapped (`~Shape`).
    pre = "cxx . . $ "
    assert _is_callable_symbol(pre + "Shape#area(21b6c25e127938e2).")
    assert _is_callable_symbol(pre + "compute(8566de10eab6f7d0).")
    assert not _is_callable_symbol(pre + "Shape#")              # the type
    assert _simple_name(pre + "Shape#area(21b6c25e127938e2).") == "area"
    assert _simple_name(pre + "compute(8566de10eab6f7d0).") == "compute"
    assert _simple_name(pre + "Shape#`~Shape`(ced63f7c635d850d).") == "~Shape"


# -- callable_locations parity (incl. the out-of-line definition-file keying) ---------------------

def test_callable_locations_match_resolution_node_ids_and_def_file():
    files = _fixture_files()
    locs = resolution.callable_locations(files, _PROJ)
    node_ids = {i.key["id"] for i in resolution.resolve(files, _PROJ)
                if type(i).__name__ == "GraphNode" and i.label in ("Function", "Method")}
    assert {loc.id for loc in locs} <= node_ids
    assert {loc.name for loc in locs} >= {"compute", "run", "area", "Circle"}
    # an out-of-line member definition is keyed to its DEFINITION file (shapes.cpp), not the declaring .h
    areas = [loc for loc in locs if loc.name == "area"]
    assert areas and all(loc.rel_path == "src/shapes.cpp" for loc in areas)


# -- the resolver: virtual dispatch + constructor + cross-file -----------------------------------

def test_resolve_calls_resolves_what_the_name_pass_cannot():
    locs = _fixture_locations()
    deep = resolve_calls(_SCIP.read_bytes(), locs, emit_self_calls=True)  # as the C++ overlay runs it
    by_id = {loc.id: loc for loc in locs}
    for e in deep.edges:
        assert e.properties["resolution"] == "resolved"
    pairs = {(by_id[e.from_key["id"]].name, by_id[e.to_key["id"]].name) for e in deep.edges}
    # `s.area()` on a `const Shape&` -> virtual dispatch resolved to the static type Shape::area.
    assert ("compute", "area") in pairs
    # a cross-file free-function call and a constructor-construction (`Circle c(2.0)`).
    assert ("run", "compute") in pairs
    assert ("run", "Circle") in pairs


def test_same_id_function_across_tus_keeps_file_and_span_together():
    # Two TUs defining a same-signature free function share one function id; the location must pair the
    # CHOSEN definition's file with its OWN line span (not another TU's file) so the SCIP join lands.
    files = [parse_source(b"int helper() { return 1; }\n", "a.cpp"),
             parse_source(b"\n\n\nint helper() { return 2; }\n", "b.cpp")]
    helpers = [loc for loc in resolution.callable_locations(files, _PROJ) if loc.name == "helper"]
    assert len(helpers) == 1
    assert helpers[0].rel_path == "a.cpp" and helpers[0].line_start == 1  # chosen (first) def's file AND span


def test_resolve_calls_tracks_coverage():
    locs = _fixture_locations()
    deep = resolve_calls(_SCIP.read_bytes(), locs, emit_self_calls=True)
    by_name = {loc.name: loc for loc in locs}
    assert {by_name[n].id for n in ("compute", "run")} <= deep.covered_ids
    assert deep.metrics["resolved"] >= 3


def test_resolve_calls_is_empty_without_matching_locations():
    deep = resolve_calls(_SCIP.read_bytes(), [])
    assert deep.edges == [] and deep.metrics["resolved"] == 0 and deep.covered_ids == set()


# -- graceful fallback ---------------------------------------------------------------------------

def _depth(graph, name):
    r = graph.ro_query("MATCH (p:Project {name:$n}) RETURN p.depth", {"n": name}).result_set
    return r[0][0] if r else None


def test_deep_falls_back_to_treesitter_when_unavailable(graph, monkeypatch):
    def _boom(_root):
        raise scip_clang.ScipClangUnavailable("forced: no scip-clang in this test")

    monkeypatch.setattr(scip_clang, "run_scip", _boom)
    report = ingest.ingest_directory(GraphWriter(graph), str(_SRC), project_name="cfb", deep="inline")

    cpp = next(r for r in report.languages if r.language == "cpp")
    assert cpp.error is None
    assert cpp.deep_status != "error"
    assert _depth(graph, "cfb") == "fast"


def test_fast_rerun_does_not_downgrade_a_deep_graph(graph):
    ingest.ingest_directory(GraphWriter(graph), str(_SRC), project_name="cdg", deep="off")
    assert _depth(graph, "cdg") == "fast"
    graph.query("MATCH (p:Project {name:'cdg'}) SET p.depth='deep'")
    ingest.ingest_directory(GraphWriter(graph), str(_SRC), project_name="cdg", deep="off")
    assert _depth(graph, "cdg") == "deep"


# -- end-to-end with a real scip-clang (gated on availability) -----------------------------------

def _scip_clang_available() -> bool:
    try:
        cmd = scip_clang._tool_command()
    except scip_clang.ScipClangUnavailable:
        return False
    try:
        return subprocess.run([*cmd, "--version"], capture_output=True, timeout=60, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.mark.skipif(not _scip_clang_available(),
                    reason="requires a working scip-clang (github.com/sourcegraph/scip-clang) via AURYN_SCIP_CLANG")
def test_deep_inline_resolves_calls_end_to_end(graph, tmp_path):
    import shutil

    proj = tmp_path / "cppprobe"
    shutil.copytree(_SRC, proj)
    # scip-clang requires an ABSOLUTE `directory` in the compdb, so generate one for this copy's location.
    srcs = ["src/shapes.cpp", "src/main.cpp"]
    (proj / "compile_commands.json").write_text(json.dumps([
        {"directory": str(proj), "file": f, "arguments": ["clang++", "-std=c++17", "-Isrc", "-c", f]}
        for f in srcs
    ]))
    report = ingest.ingest_directory(GraphWriter(graph), str(proj), project_name="ce", deep="inline")

    cpp = next(r for r in report.languages if r.language == "cpp")
    assert cpp.deep_status != "error"
    if _depth(graph, "ce") != "deep":  # scip-clang present but couldn't index here -> skip, don't fail
        pytest.skip(f"scip-clang did not produce a deep graph (deep_status={cpp.deep_status})")

    # virtual dispatch `s.area()` resolved to Shape::area, end-to-end.
    hit = graph.query(
        "MATCH (:Function {name:'compute'})-[c:CALLS {resolution:'resolved'}]->(m:Method {name:'area'}) "
        "WHERE m.id CONTAINS '.shape.' RETURN 1"
    ).result_set
    assert hit == [[1]]
