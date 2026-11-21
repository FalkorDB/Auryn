"""TypeScript/JavaScript deep pass (scip-typescript / SCIP) -- the `deep_sync` engine mirroring
Python/C#/Rust.

Most tests are pure and need no toolchain: they drive the shared SCIP reader/resolver with a committed
`index.scip` captured from a real `scip-typescript index` run (fixtures/tsprobe.scip + its source), so CI
exercises the resolution without scip-typescript installed. The end-to-end ingest test is gated on the
tool actually being available."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from auryn.core import ingest, scip
from auryn.core.graph_client import GraphWriter
from auryn.core.scip_resolve import _is_callable_symbol, _simple_name, resolve_calls
from auryn.ingestors.typescript import resolution, scip_typescript
from auryn.ingestors.typescript.file_parser import parse_source
from auryn.ingestors.typescript.ids import project_id

_FIX = Path(__file__).parent / "fixtures"
_SCIP = _FIX / "tsprobe.scip"
_SRC = _FIX / "tsprobe_src"


def _fixture_files():
    return [parse_source((_SRC / "src" / f).read_bytes(), f"src/{f}") for f in ("lib.ts", "other.ts")]


def _fixture_locations():
    return resolution.callable_locations(_fixture_files(), project_id("tsprobe"))


# -- SCIP reader (shared core reader, scip-typescript output) ------------------------------------

def test_scip_reader_decodes_documents_and_occurrences():
    docs = scip.parse_index(_SCIP.read_bytes())
    by_path = {d.relative_path: d for d in docs}
    assert set(by_path) == {"src/lib.ts", "src/other.ts"}
    lib = by_path["src/lib.ts"]
    # the cross-module callee `deep()` is referenced (not defined) in lib.ts
    refs = {_simple_name(o.symbol) for o in lib.occurrences if not o.is_definition and _is_callable_symbol(o.symbol)}
    assert "deep" in refs
    # and `helper` is defined in lib.ts
    assert any(o.is_definition and o.symbol.endswith("helper().") for o in lib.occurrences)


def test_symbol_helpers_on_typescript_symbols():
    pre = "scip-typescript npm tsprobe 1.0.0 "
    assert _is_callable_symbol(pre + "src/`lib.ts`/Fast#run().")
    assert _is_callable_symbol(pre + "src/`lib.ts`/helper().")
    assert not _is_callable_symbol(pre + "src/`lib.ts`/arrow.")       # an arrow-const term, not a callable
    assert not _is_callable_symbol(pre + "src/`lib.ts`/Fast#")        # a type
    assert not _is_callable_symbol("local 2")
    assert _simple_name(pre + "src/`lib.ts`/Fast#run().") == "run"
    assert _simple_name(pre + "src/`other.ts`/deep().") == "deep"
    assert _simple_name(pre + "src/`lib.ts`/helper().") == "helper"
    # backtick-escaped names (e.g. filenames, <constructor>, TS private members) are unwrapped
    assert _simple_name(pre + "src/`lib.ts`/Fast#`<constructor>`().") == "<constructor>"
    assert _simple_name(pre + "src/`lib.ts`/C#`#priv`().") == "#priv"


def test_symbol_helpers_handle_overload_disambiguators():
    # Java packs an overload index into the parens (`(+1)`), scip-clang a hash (`(9b79fb6aee4c0440)`);
    # both still end `).` and must yield the bare name -- the shared helpers are cross-indexer.
    assert _is_callable_symbol("scip-java maven . . minimized/Methods#overload(+1).")
    assert _is_callable_symbol("cxx . pkg v0$ arithmetic/multiply(9b79fb6aee4c0440).")
    assert _simple_name("scip-java maven . . minimized/Methods#overload(+1).") == "overload"
    assert _simple_name("cxx . pkg v0$ arithmetic/multiply(9b79fb6aee4c0440).") == "multiply"
    assert _simple_name("scip-java maven . . minimized/Methods#`<init>`().") == "<init>"
    assert not _is_callable_symbol("scip-java maven . . minimized/Methods#field.")   # a term, not a call
    assert not _is_callable_symbol("rust-analyzer cargo c 1.0 via_dyn().(x)")        # a parameter


# -- callable_locations parity (deep edges must land on the fast pass's node ids) ----------------

def test_callable_locations_match_resolution_node_ids():
    proj = project_id("tsprobe")
    files = _fixture_files()
    locs = resolution.callable_locations(files, proj)
    node_ids = {
        item.key["id"]
        for item in resolution.resolve(files, proj)
        if type(item).__name__ == "GraphNode" and item.label in ("Function", "Method")
    }
    assert {loc.id for loc in locs} <= node_ids  # every location is a real callable node
    assert {loc.name for loc in locs} >= {"caller", "helper", "deep", "run"}


# -- call-site recording (the line-precise gate's input) -----------------------------------------

def test_callable_locations_record_call_sites_at_the_callee_identifier_line():
    # The deep pass gates on (callee name, callee-identifier line). A member call's site line is the
    # PROPERTY's line -- which for a multi-line chain differs from the call expression's first line --
    # so a SCIP occurrence (anchored at the identifier) matches. A value reference is not a call site.
    src = (
        b"function caller(obj: any) {\n"       # line 1
        b"  target();\n"                        # line 2  -- call `target`
        b"  obj\n"                              # line 3  -- call expression starts here...
        b"    .method();\n"                     # line 4  -- ...but `method` is on line 4
        b"  const ref = target;\n"              # line 5  -- value reference, NOT a call site
        b"}\n"
        b"function target() {}\n"
    )
    locs = resolution.callable_locations([parse_source(src, "src/a.ts")], project_id("p"))
    caller = next(l for l in locs if l.name == "caller")
    assert ("target", 2) in caller.called_sites       # callee-identifier line of `target()`
    assert ("method", 4) in caller.called_sites       # the property line, not the line-3 expr start
    assert not any(name == "target" and line == 5 for name, line in caller.called_sites)  # value ref


# -- the resolver: inferred-receiver dispatch, same-name disambiguation, cross-module ------------

def test_resolve_calls_resolves_what_the_name_pass_cannot():
    locs = _fixture_locations()
    deep = resolve_calls(_SCIP.read_bytes(), locs, require_call_site=True)  # as the TS overlay runs it
    by_id = {loc.id: loc for loc in locs}
    for e in deep.edges:
        assert e.properties["resolution"] == "resolved"
    pairs = {(by_id[e.from_key["id"]].name, e.to_key["id"]) for e in deep.edges}

    def to_id(caller_name, contains):
        return any(name == caller_name and contains in tid for name, tid in pairs)

    # `const f = new Fast(); f.run()` -- inferred receiver, resolved to Fast#run (NOT Slow#run); the name
    # pass drops it because ".run" is ambiguous across the two classes.
    assert to_id("caller", ".class.Fast.method.run(")
    assert not to_id("caller", ".class.Slow.method.run(")
    # a cross-module import call and an intra-class free-function call
    assert to_id("caller", ".func.deep(")
    assert to_id("run", ".func.helper(")


def test_resolve_calls_tracks_coverage_and_what_scip_resolved():
    locs = _fixture_locations()
    deep = resolve_calls(_SCIP.read_bytes(), locs, require_call_site=True)
    by_name = {loc.name: loc for loc in locs}
    # every fixture callable SCIP defined is covered (the overlay scope)
    assert {by_name[n].id for n in ("caller", "helper", "deep")} <= deep.covered_ids
    # SCIP tied caller's two calls to concrete callables
    assert deep.seen_by_caller[by_name["caller"].id] == {"run", "deep"}
    assert deep.metrics["resolved"] == 3 and deep.metrics["external"] == 0


def test_value_reference_is_not_emitted_as_a_call_when_gated():
    # `getHelper` returns `helper` as a VALUE (no call). Without gating, SCIP's reference becomes a
    # spurious CALLS edge; both the name gate and the line-precise call-site gate (the overlay's) drop it.
    locs = _fixture_locations()
    by_name = {loc.name: loc for loc in locs}
    gh = by_name["getHelper"].id
    ungated = {(e.from_key["id"], e.to_key["id"]) for e in resolve_calls(_SCIP.read_bytes(), locs).edges}
    assert (gh, by_name["helper"].id) in ungated      # the false positive the gates remove
    for gate in ({"require_syntactic_call": True}, {"require_call_site": True}):
        gated = {(e.from_key["id"], e.to_key["id"])
                 for e in resolve_calls(_SCIP.read_bytes(), locs, **gate).edges}
        assert not any(frm == gh for frm, _ in gated)  # gated: getHelper makes no CALLS


def test_self_recursion_is_resolved_under_emit_self_calls():
    # The TS overlay runs with emit_self_calls=True, so the resolver authors the recursive self-edge
    # (fib -> fib) -- precisely replacing the fast guess and surviving even a dropped duplicate-name guess.
    # Without the flag (Rust's mode, whose fast pass omits self-CALLS) no self-edge is emitted.
    locs = _fixture_locations()
    fib = {loc.id for loc in locs if loc.name == "fib"}

    def has_self(**kw):
        return any(e.from_key["id"] in fib and e.to_key["id"] in fib
                   for e in resolve_calls(_SCIP.read_bytes(), locs, **kw).edges)

    assert has_self(require_call_site=True, emit_self_calls=True)   # as the TS overlay runs it
    assert not has_self(require_call_site=True)


def test_resolve_calls_is_empty_without_matching_locations():
    # SCIP for a different project (no overlapping files) resolves nothing -- never crashes.
    deep = resolve_calls(_SCIP.read_bytes(), [])
    assert deep.edges == [] and deep.metrics["resolved"] == 0 and deep.covered_ids == set()


# -- graceful fallback: deep mode with the tool unavailable yields the fast graph ----------------

def _depth(graph, name):
    r = graph.ro_query("MATCH (p:Project {name:$n}) RETURN p.depth", {"n": name}).result_set
    return r[0][0] if r else None


def test_deep_falls_back_to_treesitter_when_unavailable(graph, monkeypatch):
    def _boom(_root):
        raise scip_typescript.ScipTypescriptUnavailable("forced: no scip-typescript in this test")

    monkeypatch.setattr(scip_typescript, "run_scip", _boom)
    report = ingest.ingest_directory(GraphWriter(graph), str(_SRC), project_name="tsfb", deep="inline")

    ts = next(r for r in report.languages if r.language == "typescript")
    assert ts.error is None
    assert _depth(graph, "tsfb") == "fast"  # deep couldn't run -> stayed at the fast graph


def test_fast_rerun_does_not_downgrade_a_deep_graph(graph):
    # Simulate a completed deep pass by stamping the marker, then prove a fast re-run no-ops (the depth
    # short-circuit) rather than overwriting depth back to "fast" -- no scip-typescript needed.
    ingest.ingest_directory(GraphWriter(graph), str(_SRC), project_name="tsdg", deep="off")
    assert _depth(graph, "tsdg") == "fast"
    graph.query("MATCH (p:Project {name:'tsdg'}) SET p.depth='deep'")
    ingest.ingest_directory(GraphWriter(graph), str(_SRC), project_name="tsdg", deep="off")
    assert _depth(graph, "tsdg") == "deep"  # a fast pass must never downgrade a deep graph


# -- end-to-end with a real scip-typescript (gated on availability) ------------------------------

def _scip_typescript_available() -> bool:
    try:
        cmd = scip_typescript._tool_command()
    except scip_typescript.ScipTypescriptUnavailable:
        return False
    try:
        return subprocess.run([*cmd, "--version"], capture_output=True, timeout=120, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.mark.skipif(not _scip_typescript_available(),
                    reason="requires a working scip-typescript (npm i -g @sourcegraph/scip-typescript)")
def test_deep_inline_resolves_calls_end_to_end(graph, tmp_path):
    import shutil

    proj = tmp_path / "tsprobe"
    shutil.copytree(_SRC, proj)
    report = ingest.ingest_directory(GraphWriter(graph), str(proj), project_name="tse", deep="inline")

    ts = next(r for r in report.languages if r.language == "typescript")
    assert ts.deep_status == "inline"
    assert _depth(graph, "tse") == "deep"  # a working scip-typescript upgraded the graph in place

    # the inferred-receiver method dispatch the name pass can't make: caller -> Fast#run, resolved to the
    # CONCRETE class (not Slow#run).
    hit = graph.query(
        "MATCH (:Function {name:'caller'})-[c:CALLS {resolution:'resolved'}]->(m:Method {name:'run'}) "
        "WHERE m.id CONTAINS '.class.Fast.method.run' RETURN 1"
    ).result_set
    assert hit == [[1]]
    # and no resolved edge to the wrong same-named method
    wrong = graph.query(
        "MATCH (:Function {name:'caller'})-[:CALLS]->(m:Method) WHERE m.id CONTAINS '.class.Slow.method.run' "
        "RETURN count(*)"
    ).result_set
    assert wrong == [[0]]
    # recursion survives deep (the self CALLS edge is preserved, not dropped by the overlay)
    rec = graph.query("MATCH (f:Function {name:'fib'})-[:CALLS]->(f) RETURN 1").result_set
    assert rec == [[1]]
    # a function used as a VALUE (`return helper`) is not a call -- no CALLS edge from getHelper
    val = graph.query(
        "MATCH (:Function {name:'getHelper'})-[:CALLS]->(:Function {name:'helper'}) RETURN count(*)"
    ).result_set
    assert val == [[0]]


@pytest.mark.skipif(not _scip_typescript_available(),
                    reason="requires a working scip-typescript (npm i -g @sourcegraph/scip-typescript)")
def test_deep_recovers_an_aliased_import_call_end_to_end(graph, tmp_path):
    # `import { helper as h }; h()` -- the fast parse records the alias `h`, so a name-only gate (and the
    # fast pass itself) can't tie the call to `helper`. The line-precise call-site gate accepts the sole
    # call on that line, so deep resolves it. Prove it's a DEEP win: fast-only has no such edge.
    proj = tmp_path / "aliaspkg"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "helpers.ts").write_text("export function helper() { return 1; }\n")
    (proj / "src" / "main.ts").write_text(
        'import { helper as h } from "./helpers";\n'
        "export function caller() {\n"
        "  return h();\n"
        "}\n"
    )

    def _caller_to_helper(name):
        return graph.query(
            "MATCH (:Function {name:'caller'})-[e:CALLS]->(t:Function {name:'helper'}) "
            f"WHERE t.id CONTAINS '{name}' RETURN e.resolution"
        ).result_set

    ingest.ingest_directory(GraphWriter(graph), str(proj), project_name="aliasfast", deep="off")
    assert _caller_to_helper("aliasfast") == []  # the fast pass can't resolve the aliased import call

    ingest.ingest_directory(GraphWriter(graph), str(proj), project_name="aliasdeep", deep="inline")
    assert _depth(graph, "aliasdeep") == "deep"
    assert _caller_to_helper("aliasdeep") == [["resolved"]]  # deep recovers it through the alias
