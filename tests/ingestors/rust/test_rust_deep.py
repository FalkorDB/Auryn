"""Rust deep pass (rust-analyzer / SCIP) -- the `deep_sync` engine mirroring Python/C#.

Most tests are pure and need no toolchain: they drive the SCIP reader and the call resolver with a
committed `index.scip` captured from a real `rust-analyzer scip` run (fixtures/raprobe.scip + its
source), so CI exercises the resolution without rust-analyzer installed. The end-to-end ingest test is
gated on the tool actually being available."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from auryn.core import ingest
from auryn.core.graph_client import GraphWriter
from auryn.ingestors.rust import resolution, rust_analyzer, scip
from auryn.ingestors.rust.file_parser import parse_source
from auryn.ingestors.rust.ids import project_id

_FIX = Path(__file__).parent / "fixtures"
_SCIP = _FIX / "raprobe.scip"
_SRC = _FIX / "raprobe_src"


def _fixture_locations():
    raws = [parse_source((_SRC / "src" / f).read_bytes(), f"src/{f}") for f in ("lib.rs", "other.rs")]
    return resolution.callable_locations(raws, project_id("raprobe"))


# -- SCIP reader --------------------------------------------------------------------------------

def test_scip_reader_decodes_documents_and_occurrences():
    docs = scip.parse_index(_SCIP.read_bytes())
    by_path = {d.relative_path: d for d in docs}
    assert set(by_path) == {"src/lib.rs", "src/other.rs"}
    lib = by_path["src/lib.rs"]
    # the cross-module callee `other::deep()` is referenced (not defined) in lib.rs
    refs = {(o.symbol.split(" ", 4)[-1], o.start_line) for o in lib.occurrences if not o.is_definition}
    assert ("other/deep().", 19) in refs
    # and `helper` is defined in lib.rs
    assert any(o.is_definition and o.symbol.endswith("helper().") for o in lib.occurrences)


def test_symbol_helpers():
    assert rust_analyzer._is_callable_symbol("rust-analyzer cargo c 1.0 Fast#Run#run().")
    assert not rust_analyzer._is_callable_symbol("rust-analyzer cargo c 1.0 via_dyn().(x)")  # a param
    assert not rust_analyzer._is_callable_symbol("local 7")
    assert rust_analyzer._simple_name("rust-analyzer cargo c 1.0 Fast#Run#run().") == "run"
    assert rust_analyzer._simple_name("rust-analyzer cargo c 1.0 other/deep().") == "deep"
    assert rust_analyzer._simple_name("rust-analyzer cargo c 1.0 helper().") == "helper"


# -- callable_locations parity (deep edges must land on the fast pass's node ids) ----------------

def test_callable_locations_match_resolution_node_ids():
    raws = [parse_source((_SRC / "src" / f).read_bytes(), f"src/{f}") for f in ("lib.rs", "other.rs")]
    proj = project_id("raprobe")
    locs = resolution.callable_locations(raws, proj)
    node_ids = {
        item.key["id"]
        for item in resolution.resolve(raws, proj)
        if type(item).__name__ == "GraphNode" and item.label in ("Function", "Method")
    }
    assert {loc.id for loc in locs} <= node_ids  # every location is a real callable node
    assert {loc.name for loc in locs} >= {"caller", "helper", "deep", "via_dyn", "via_generic", "run"}


# -- the resolver: dynamic / generic / concrete-impl / cross-module calls ------------------------

def test_resolve_calls_resolves_what_the_name_pass_cannot():
    locs = _fixture_locations()
    deep = rust_analyzer.resolve_calls(_SCIP.read_bytes(), locs)
    assert deep.metrics["resolved"] == 5 and deep.metrics["external"] == 0
    by_id = {loc.id: loc for loc in locs}
    pairs = {(by_id[e.from_key["id"]].name, e.to_key["id"]) for e in deep.edges}
    for e in deep.edges:
        assert e.properties["resolution"] == "resolved"

    def to_id(caller_name, contains):
        return any(name == caller_name and contains in tid for name, tid in pairs)

    # `let f = Fast; f.run()` -- untyped local, resolved to the CONCRETE impl method (the name pass drops it)
    assert to_id("caller", ".struct.fast.method.run")
    # dynamic (`&dyn Run`) and generic (`T: Run`) dispatch -> the trait method (best static target)
    assert to_id("via_dyn", ".trait.run.method.run")
    assert to_id("via_generic", ".trait.run.method.run")
    # a free call and a cross-module `other::deep()` call
    assert to_id("caller", ".fn.helper(")
    assert to_id("caller", ".fn.deep(")


def test_resolve_calls_tracks_coverage_and_what_scip_resolved():
    locs = _fixture_locations()
    deep = rust_analyzer.resolve_calls(_SCIP.read_bytes(), locs)
    by_name = {loc.name: loc for loc in locs}
    # every fixture callable is in the active crate, so all are covered (overlay scope)
    assert deep.covered_ids == {loc.id for loc in locs}
    # SCIP tied `caller`'s three calls to concrete callables, so none remain unresolved after deep
    assert deep.seen_by_caller[by_name["caller"].id] == {"run", "helper", "deep"}
    unresolved = len(by_name["caller"].called_names - deep.seen_by_caller[by_name["caller"].id])
    assert unresolved == 0


def test_fn_pointer_call_stays_unresolved_after_deep():
    # `calls_param(f: fn()) { f() }` -- SCIP ties f() to the parameter, not a callable, so deep can't
    # resolve it; the recomputed unresolved_calls must stay >= 1 (not blindly zeroed).
    locs = _fixture_locations()
    deep = rust_analyzer.resolve_calls(_SCIP.read_bytes(), locs)
    cp = next(loc for loc in locs if loc.name == "calls_param")
    assert cp.id in deep.covered_ids
    assert "f" not in deep.seen_by_caller.get(cp.id, set())
    assert len(cp.called_names - deep.seen_by_caller.get(cp.id, set())) >= 1


def test_resolve_calls_is_empty_without_matching_locations():
    # SCIP for a different project (no overlapping files) resolves nothing -- never crashes.
    deep = rust_analyzer.resolve_calls(_SCIP.read_bytes(), [])
    assert deep.edges == [] and deep.metrics["resolved"] == 0 and deep.covered_ids == set()


# -- graceful fallback: deep mode with the tool unavailable yields the fast graph ----------------

def _depth(graph, name):
    r = graph.ro_query("MATCH (p:Project {name:$n}) RETURN p.depth", {"n": name}).result_set
    return r[0][0] if r else None


def test_deep_falls_back_to_treesitter_when_unavailable(graph, monkeypatch):
    def _boom(_root):
        raise rust_analyzer.RustAnalyzerUnavailable("forced: no rust-analyzer in this test")

    monkeypatch.setattr(rust_analyzer, "run_scip", _boom)
    report = ingest.ingest_directory(GraphWriter(graph), str(_SRC), project_name="rafb", deep="inline")

    rust = next(r for r in report.languages if r.language == "rust")
    assert rust.error is None
    assert _depth(graph, "rafb") == "fast"  # deep couldn't run -> stayed at the fast graph
    # the fast pass's honesty signal is intact (unresolved_calls present on a generic-dispatch fn)
    r = graph.ro_query("MATCH (f:Function {name:'via_generic'}) RETURN f.unresolved_calls").result_set
    assert r and r[0][0] >= 1


def test_fast_rerun_does_not_downgrade_a_deep_graph(graph):
    # Simulate a completed deep pass by stamping the marker, then prove a fast re-run no-ops (the
    # depth short-circuit) rather than overwriting depth back to "fast" -- no rust-analyzer needed.
    ingest.ingest_directory(GraphWriter(graph), str(_SRC), project_name="rank", deep="off")
    assert _depth(graph, "rank") == "fast"
    graph.query("MATCH (p:Project {name:'rank'}) SET p.depth='deep'")
    ingest.ingest_directory(GraphWriter(graph), str(_SRC), project_name="rank", deep="off")
    assert _depth(graph, "rank") == "deep"  # a fast pass must never downgrade a deep graph


# -- end-to-end with a real rust-analyzer (gated on availability) --------------------------------

def _rust_analyzer_available() -> bool:
    # Must actually RUN: a `rustup` proxy (`~/.cargo/bin/rust-analyzer`) resolves on PATH even when the
    # component isn't installed, and only fails when invoked (CI hits exactly this). Probe `--version`.
    try:
        cmd = rust_analyzer._tool_command()
    except rust_analyzer.RustAnalyzerUnavailable:
        return False
    try:
        return subprocess.run([*cmd, "--version"], capture_output=True, timeout=60, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.mark.skipif(not _rust_analyzer_available(), reason="requires a working rust-analyzer (rustup component add rust-analyzer)")
def test_deep_inline_resolves_calls_end_to_end(graph, tmp_path):
    # Copy the cargo fixture out of the repo (rust-analyzer writes a target/ dir) and ingest it deep.
    proj = tmp_path / "raprobe"
    shutil.copytree(_SRC, proj)
    report = ingest.ingest_directory(GraphWriter(graph), str(proj), project_name="rae", deep="inline")

    rust = next(r for r in report.languages if r.language == "rust")
    assert rust.deep_status == "inline"
    assert _depth(graph, "rae") == "deep"  # a working rust-analyzer upgraded the graph in place

    # the concrete-impl call the name pass can't make: caller -> Fast::run, resolved
    hit = graph.query(
        "MATCH (:Function {name:'caller'})-[c:CALLS {resolution:'resolved'}]->(m:Method {name:'run'}) "
        "WHERE m.id CONTAINS '.struct.fast.method.run' RETURN 1"
    ).result_set
    assert hit == [[1]]
    # a fn-pointer parameter call deep CANNOT resolve stays counted (not blindly zeroed)
    rp = graph.ro_query("MATCH (f:Function {name:'calls_param'}) RETURN f.unresolved_calls").result_set
    assert rp and rp[0][0] >= 1
    # deep resolution zeroes the fast pass's incompleteness count
    r = graph.ro_query("MATCH (f:Function {name:'via_generic'}) RETURN f.unresolved_calls").result_set
    assert r and r[0][0] == 0
