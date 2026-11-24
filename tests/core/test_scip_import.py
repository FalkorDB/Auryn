"""The generic SCIP importer -- build a structural graph directly from any indexer's index.scip (no fast pass).

Driven by the committed fixtures the per-language deep tests already capture from real indexers
(scip-typescript / scip-go / scip-clang), so this exercises the cross-indexer import with no toolchain."""

from __future__ import annotations

from pathlib import Path

from auryn.core import scip_import

_FIXTURES = Path(__file__).resolve().parents[1] / "ingestors"
_TS = _FIXTURES / "typescript" / "fixtures" / "tsprobe.scip"
_GO = _FIXTURES / "go" / "fixtures" / "goprobe.scip"
_CPP = _FIXTURES / "cpp" / "fixtures" / "cppprobe.scip"


def _build(path: Path, proj: str):
    items = list(scip_import.build_graph(path.read_bytes(), proj, project_name=proj))
    nodes = [i for i in items if type(i).__name__ == "GraphNode"]
    edges = [i for i in items if type(i).__name__ == "GraphEdge"]
    return nodes, edges


def _by_label(nodes, label):
    return {n.properties["name"] for n in nodes if n.label == label}


def test_typescript_index_builds_files_callables_and_types():
    nodes, edges = _build(_TS, "p")
    assert {n.properties["path"] for n in nodes if n.label == "File"} == {"src/lib.ts", "src/other.ts"}
    assert all(n.properties["language"] == "typescript" for n in nodes if n.label == "File")  # normalized casing
    assert _by_label(nodes, "Type") == {"Fast", "Slow"}
    assert {"helper", "deep", "caller"} <= _by_label(nodes, "Function")
    assert "run" in _by_label(nodes, "Method")  # Fast#run / Slow#run -- a method (has a type ancestor)
    assert any(e.rel_type == "HAS_FILE" for e in edges)
    assert any(e.rel_type == "DEFINED_IN" for e in edges)


def test_go_index_builds_the_package_types_and_methods():
    nodes, edges = _build(_GO, "p")
    assert _by_label(nodes, "Type") >= {"Service", "Store", "Repository"}
    assert _by_label(nodes, "Method") >= {"Save", "Count", "Put"}
    assert _by_label(nodes, "Function") >= {"Run", "NewService", "New"}
    assert all(n.properties["language"] == "go" for n in nodes if n.label == "File")


def test_cpp_index_builds_classes_methods_and_free_functions():
    nodes, _ = _build(_CPP, "p")
    assert _by_label(nodes, "Type") == {"Shape", "Circle"}
    assert {"area", "~Shape"} <= _by_label(nodes, "Method")  # incl. the backtick-escaped destructor
    assert _by_label(nodes, "Function") == {"compute", "run"}


def test_has_method_links_a_method_to_its_defined_type():
    nodes, edges = _build(_GO, "p")
    by_id = {n.key["id"]: n for n in nodes}
    has_method = [e for e in edges if e.rel_type == "HAS_METHOD"]
    assert has_method
    for e in has_method:  # every HAS_METHOD goes Type -> Method, both real defined nodes
        assert by_id[e.from_key["id"]].label == "Type"
        assert by_id[e.to_key["id"]].label == "Method"
    # Service HAS_METHOD Save specifically
    save = next(n for n in nodes if n.label == "Method" and n.properties["name"] == "Save")
    svc = next(n for n in nodes if n.label == "Type" and n.properties["name"] == "Service")
    assert any(e.from_key["id"] == svc.key["id"] and e.to_key["id"] == save.key["id"] for e in has_method)


def test_entities_link_to_their_file_the_way_impact_of_diff_expects():
    # impact_of_diff finds a file's symbols by a `file_path` property (C/C++/TS style) OR a DEFINED_IN edge
    # (Go/Java/Rust/PHP style), both gated on line_start/line_end. The importer must satisfy both so a
    # generic-imported graph is first-class in that tooling (and get_context's edge-agnostic 1-hop).
    nodes, edges = _build(_TS, "p")
    files = {n.key["id"]: n.properties["path"] for n in nodes if n.label == "File"}
    defined_in = {(e.from_key["id"], e.to_key["id"]) for e in edges if e.rel_type == "DEFINED_IN"}
    for n in nodes:
        if n.label in ("Function", "Method", "Type"):
            # (a) the file_path property names a real File's path
            assert n.properties["file_path"] in files.values()
            # (b) line_start/line_end are both present (the impact line-range gate needs both)
            assert n.properties["line_start"] >= 1 and n.properties["line_end"] >= n.properties["line_start"]
            # (c) a DEFINED_IN edge points at the File whose path matches file_path
            targets = [files[fid] for (src, fid) in defined_in if src == n.key["id"]]
            assert targets == [n.properties["file_path"]]


def test_project_node_and_scoped_stable_ids():
    nodes, _ = _build(_TS, "myproj")
    proj = [n for n in nodes if n.label == "Project"]
    assert len(proj) == 1 and proj[0].key["id"] == "myproj"
    # symbol node ids are project-scoped (so the same index imports into two graphs without collision)
    assert all(n.key["id"].startswith("myproj::") for n in nodes if n.label in ("Function", "Method", "Type"))
    # deterministic: re-importing yields identical ids
    again, _ = _build(_TS, "myproj")
    assert {n.key["id"] for n in nodes} == {n.key["id"] for n in again}


def test_locals_and_terms_are_not_emitted_as_callables():
    # `local N` (function-scoped) and term descriptors (Go struct fields / consts) are not callables/types
    nodes, _ = _build(_GO, "p")
    names = {n.properties["name"] for n in nodes if n.label in ("Function", "Method", "Type")}
    assert "svc" not in names and "err" not in names          # locals
    assert {"store", "id", "MaxItems"}.isdisjoint(names)      # struct fields / a const -> TERM, not emitted
