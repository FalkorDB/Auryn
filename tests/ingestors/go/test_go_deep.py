"""Go deep pass (scip-go / SCIP) -- the `deep_sync` engine mirroring Python/C#/Rust/TypeScript.

Most tests are pure and need no toolchain: they drive the shared SCIP reader/resolver with a committed
`index.scip` captured from a real `scip-go index` run (fixtures/goprobe.scip + its source module), so CI
exercises the resolution without scip-go installed. The end-to-end ingest test is gated on the tool
actually being available."""

from __future__ import annotations

import glob
import os
import subprocess
from pathlib import Path

import pytest
from auryn.core import ingest, scip
from auryn.core.graph_client import GraphWriter
from auryn.core.scip_resolve import _is_callable_symbol, _simple_name, resolve_calls
from auryn.ingestors.go import resolution, scip_go
from auryn.ingestors.go.file_parser import parse_source
from auryn.ingestors.go.ids import project_id

_FIX = Path(__file__).parent / "fixtures"
_SCIP = _FIX / "goprobe.scip"
_SRC = _FIX / "goprobe_src"
_PROJ = project_id("goprobe")


def _fixture_files():
    return [parse_source((_SRC / rel).read_bytes(), rel)
            for rel in sorted(os.path.relpath(p, _SRC) for p in glob.glob(str(_SRC / "**/*.go"), recursive=True))]


def _fixture_locations():
    return resolution.callable_locations(_fixture_files(), _PROJ)


# -- SCIP reader (shared core reader; scip-go uses the TYPED single_line_range, not the deprecated one) --

def test_scip_reader_decodes_typed_ranges():
    docs = scip.parse_index(_SCIP.read_bytes())
    assert {d.relative_path for d in docs} >= {"app/service.go", "model/store.go"}
    # scip-go emits single_line_range(8)/multi_line_range(9); the reader must still surface occurrences
    total = sum(len(d.occurrences) for d in docs)
    assert total > 0
    svc = next(d for d in docs if d.relative_path == "app/service.go")
    assert any(o.is_definition and o.symbol.endswith("Save().") for o in svc.occurrences)


def test_symbol_helpers_on_go_symbols():
    pre = "scip-go gomod example.com/acme/proj 4b6486384ae2 "
    assert _is_callable_symbol(pre + "`example.com/acme/proj/app`/Run().")
    assert _is_callable_symbol(pre + "`example.com/acme/proj/app`/Service#Save().")
    assert not _is_callable_symbol(pre + "`example.com/acme/proj/app`/Service#")   # the type
    assert not _is_callable_symbol("local 2")
    # the backtick-wrapped import path holds `/` and `.`, which must NOT be mistaken for separators
    assert _simple_name(pre + "`example.com/acme/proj/app`/Run().") == "Run"
    assert _simple_name(pre + "`example.com/acme/proj/app`/Service#Save().") == "Save"
    assert _simple_name(pre + "`example.com/acme/proj/model`/New().") == "New"


# -- callable_locations parity (deep edges must land on the fast pass's node ids) -----------------

def test_callable_locations_match_resolution_node_ids():
    files = _fixture_files()
    locs = resolution.callable_locations(files, _PROJ)
    node_ids = {
        item.key["id"]
        for item in resolution.resolve(files, _PROJ, {})
        if type(item).__name__ == "GraphNode" and item.label in ("Function", "Method")
    }
    assert {loc.id for loc in locs} <= node_ids  # every location is a real callable node
    assert {loc.name for loc in locs} >= {"Run", "NewService", "Save", "Count", "New", "Put"}


# -- the resolver: receiver-typed method dispatch + cross-package resolution ----------------------

def test_resolve_calls_resolves_what_the_name_pass_cannot():
    locs = _fixture_locations()
    deep = resolve_calls(_SCIP.read_bytes(), locs)  # as the Go overlay runs it (no syntactic-call gate)
    by_id = {loc.id: loc for loc in locs}
    for e in deep.edges:
        assert e.properties["resolution"] == "resolved"
    pairs = {(by_id[e.from_key["id"]].name, e.to_key["id"]) for e in deep.edges}

    def to_id(caller, contains):
        return any(name == caller and contains in tid for name, tid in pairs)

    # `svc.Save()` / `svc.Count()` -- receiver-typed method dispatch the fast pass can't type (it emits no
    # such edge at all); deep resolves both to the concrete Service methods.
    assert to_id("Run", ".struct.Service.method.Save(")
    assert to_id("Run", ".struct.Service.method.Count(")
    # chained-selector calls (`svc.store.Put()` / `svc.store.Size()`) -- the fast parser omits these (the
    # receiver is too complex to name), so deep is the ONLY source of these method-dispatch edges.
    assert to_id("Save", ".struct.Store.method.Put(")
    assert to_id("Count", ".struct.Store.method.Size(")
    # `model.New()` -- a cross-PACKAGE call; deep resolves it to the in-source function.
    assert to_id("NewService", ".func.New(")


def test_resolve_calls_tracks_coverage():
    locs = _fixture_locations()
    deep = resolve_calls(_SCIP.read_bytes(), locs)
    by_name = {loc.name: loc for loc in locs}
    assert {by_name[n].id for n in ("Run", "NewService", "Save")} <= deep.covered_ids
    assert deep.metrics["resolved"] >= 5 and deep.metrics["external"] >= 1  # genuine stdlib externals kept


def test_resolve_calls_is_empty_without_matching_locations():
    deep = resolve_calls(_SCIP.read_bytes(), [])
    assert deep.edges == [] and deep.metrics["resolved"] == 0 and deep.covered_ids == set()


# -- graceful fallback -----------------------------------------------------------------------------

def _depth(graph, name):
    r = graph.ro_query("MATCH (p:Project {name:$n}) RETURN p.depth", {"n": name}).result_set
    return r[0][0] if r else None


def test_deep_falls_back_to_treesitter_when_unavailable(graph, monkeypatch):
    def _boom(_root):
        raise scip_go.ScipGoUnavailable("forced: no scip-go in this test")

    monkeypatch.setattr(scip_go, "run_scip", _boom)
    report = ingest.ingest_directory(GraphWriter(graph), str(_SRC), project_name="gofb", deep="inline")

    go = next(r for r in report.languages if r.language == "go")
    assert go.error is None
    assert go.deep_status != "error"  # ScipGoUnavailable is HANDLED (not an escaped error)
    assert _depth(graph, "gofb") == "fast"  # deep couldn't run -> stayed at the fast graph


def test_fast_rerun_does_not_downgrade_a_deep_graph(graph):
    ingest.ingest_directory(GraphWriter(graph), str(_SRC), project_name="godg", deep="off")
    assert _depth(graph, "godg") == "fast"
    graph.query("MATCH (p:Project {name:'godg'}) SET p.depth='deep'")
    ingest.ingest_directory(GraphWriter(graph), str(_SRC), project_name="godg", deep="off")
    assert _depth(graph, "godg") == "deep"  # a fast pass must never downgrade a deep graph


# -- end-to-end with a real scip-go (gated on availability) --------------------------------------

def _scip_go_available() -> bool:
    try:
        cmd = scip_go._tool_command()
    except scip_go.ScipGoUnavailable:
        return False
    try:
        return subprocess.run([*cmd, "--version"], capture_output=True, timeout=120, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.mark.skipif(not _scip_go_available(),
                    reason="requires a working scip-go (go install github.com/scip-code/scip-go/cmd/scip-go@latest)")
def test_deep_inline_resolves_calls_end_to_end(graph, tmp_path):
    import shutil

    proj = tmp_path / "goprobe"
    shutil.copytree(_SRC, proj)
    report = ingest.ingest_directory(GraphWriter(graph), str(proj), project_name="goe", deep="inline")

    go = next(r for r in report.languages if r.language == "go")
    assert go.deep_status == "inline"
    assert _depth(graph, "goe") == "deep"  # a working scip-go upgraded the graph in place

    # receiver-typed method dispatch the name pass can't make: Run -> Service.Save, resolved.
    hit = graph.query(
        "MATCH (:Function {name:'Run'})-[c:CALLS {resolution:'resolved'}]->(m:Method {name:'Save'}) "
        "WHERE m.id CONTAINS '.struct.Service.method.Save' RETURN 1"
    ).result_set
    assert hit == [[1]]
    # a chained-selector call (`svc.store.Put()`) the fast parser omits entirely -- deep is its only source.
    chained = graph.query(
        "MATCH (:Method {name:'Save'})-[:CALLS {resolution:'resolved'}]->(m:Method {name:'Put'}) "
        "WHERE m.id CONTAINS '.struct.Store.method.Put' RETURN 1"
    ).result_set
    assert chained == [[1]]
    # cross-package model.New resolved in-source.
    real = graph.query(
        "MATCH (:Function {name:'NewService'})-[:CALLS {resolution:'resolved'}]->(f:Function {name:'New'}) "
        "WHERE f.id CONTAINS '.func.New' RETURN 1"
    ).result_set
    assert real == [[1]]
