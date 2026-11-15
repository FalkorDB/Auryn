"""Integration tests for C ingestion wiring, against the real `graph` fixture. Proves the C
ingestor is registered as the owner of `.c`/`.h` files in the multi-language endpoint and
that the fast tree-sitter engine models C's linkage rules end-to-end on the demo sample:
one Function entity shared by the header that DECLARES it and the .c that DEFINES it,
static-vs-extern call resolution, project-internal #include resolution, and type REFERENCES.
Needs FalkorDB (the conftest fixture)."""

from __future__ import annotations

import shutil
from pathlib import Path

import git

from auryn.core import ingest
from auryn.core.graph_client import GraphWriter

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SAMPLE = _REPO_ROOT / "demo" / "c-ingestor" / "sample"


def _ingest(graph):
    ingest.ingest_directory(GraphWriter(graph), str(_SAMPLE), project_name="shapes")

    def q(cypher):
        return graph.query(cypher).result_set

    return q


def test_ingest_directory_owns_c(graph):
    report = ingest.ingest_directory(GraphWriter(graph), str(_SAMPLE), project_name="shapes")

    ingested = {r.language: r for r in report.languages if r.result is not None}
    assert "c" in ingested
    assert ingested["c"].files_detected == 3  # shapes.h + shapes.c + main.c
    assert not any(u["language"] == "c" for u in report.unsupported_detected)


def test_types_fields_enums(graph):
    q = _ingest(graph)
    assert sorted(r[0] for r in q("MATCH (:Struct {name:'Point'})-[:HAS_FIELD]->(f) RETURN f.name")) == ["x", "y"]
    assert sorted(r[0] for r in q("MATCH (:Struct {name:'Shape'})-[:HAS_FIELD]->(f) RETURN f.name")) == ["kind", "origin", "size"]
    assert {r[0] for r in q("MATCH (t:Typedef) RETURN t.name")} >= {"Point", "Shape"}
    assert set(q("MATCH (e:Enum {name:'ShapeKind'}) RETURN e.members")[0][0]) == {"SHAPE_CIRCLE", "SHAPE_SQUARE"}


def test_function_linkage_declare_define_merge(graph):
    q = _ingest(graph)
    # ONE external Function entity, declared in the header, defined in the .c -- not two nodes.
    assert q("MATCH (f:Function {name:'shape_area'}) RETURN count(f), f.defined, f.linkage") == [[1, True, "external"]]
    assert q("MATCH (:File {name:'shapes.h'})-[:DECLARES]->(:Function {name:'shape_area'}) RETURN 1") == [[1]]
    assert q("MATCH (:File {name:'shapes.c'})-[:DEFINES]->(:Function {name:'shape_area'}) RETURN 1") == [[1]]
    assert q("MATCH (f:Function {name:'square'}) RETURN f.linkage") == [["internal"]]  # static


def test_calls_resolve_static_first_then_extern(graph):
    q = _ingest(graph)
    # shape_area -> square: resolves to the file-local static (internal linkage shadows).
    assert q("MATCH (:Function {name:'shape_area'})-[:CALLS]->(s:Function {name:'square'}) RETURN s.linkage") == [["internal"]]
    # main -> the two project externs + printf, now surfaced as a libc placeholder (ADR 0004).
    assert sorted(r[0] for r in q("MATCH (:Function {name:'main'})-[:CALLS]->(f) RETURN f.name")) == ["point_make", "printf", "shape_area"]
    # printf is a placeholder: resolved=false, origin=libc, keyed by its cross-repo linkage.
    assert q("MATCH (:Function {name:'main'})-[:CALLS]->(p {name:'printf'}) "
             "RETURN p.id, p.resolved, p.origin") == [["extern://c/printf", False, "libc"]]


def test_includes_resolved_within_project(graph):
    q = _ingest(graph)
    assert sorted(r[0] for r in q("MATCH (a:File)-[:INCLUDES]->(:File {name:'shapes.h'}) RETURN a.name")) == ["main.c", "shapes.c"]
    # <stdio.h> is a system header outside the project -> no File node, no INCLUDES edge.
    assert q("MATCH (:File)-[:INCLUDES]->(:File {name:'stdio.h'}) RETURN 1") == []


def test_type_references(graph):
    q = _ingest(graph)
    assert q("MATCH (:Function {name:'point_make'})-[:REFERENCES]->({name:'Point'}) RETURN 1") == [[1]]  # return type
    assert q("MATCH (:Function {name:'shape_area'})-[:REFERENCES]->({name:'Shape'}) RETURN 1") == [[1]]  # param type
    assert q("MATCH (:Struct {name:'Shape'})-[:REFERENCES]->({name:'Point'}) RETURN 1") == [[1]]  # field type


def test_resync_same_content_is_a_noop(graph):
    ingest.ingest_directory(GraphWriter(graph), str(_SAMPLE), project_name="shapes")
    report = ingest.ingest_directory(GraphWriter(graph), str(_SAMPLE), project_name="shapes")
    r = next(x.result for x in report.languages if x.language == "c")
    assert (r.nodes_added, r.nodes_removed, r.edges_added, r.edges_removed) == (0, 0, 0, 0)


def test_full_recompute_prunes_dropped_extern_call_and_orphan(graph, tmp_path):
    """A full recompute (plain-dir re-ingest -> stream_apply) must drop a `CALLS -> extern://`
    placeholder edge a caller no longer makes, and GC the placeholder once it loses its last
    referrer -- exactly as a fresh ingest would. Regression: read_fingerprints scoped the edge
    read to in-prefix targets, so `extern://` edges were never seen as current and lingered
    forever (stale edge + orphan node; incremental != full)."""
    src = tmp_path / "proj"
    src.mkdir()
    f = src / "a.c"
    f.write_text("void helper(void) { undefined_external_fn(); }\n")
    ingest.ingest_directory(GraphWriter(graph), str(src), project_name="prj")

    # After v1: the unresolved call is a placeholder edge + node.
    assert graph.query("MATCH (:Function {name:'helper'})-[:CALLS]->"
                       "(e {id:'extern://c/undefined_external_fn'}) RETURN 1").result_set == [[1]]
    assert graph.query("MATCH (e {id:'extern://c/undefined_external_fn'}) RETURN 1").result_set == [[1]]

    # v2 drops the call -> full recompute must remove both the edge and the now-orphan placeholder.
    f.write_text("void helper(void) { }\n")
    ingest.ingest_directory(GraphWriter(graph), str(src), project_name="prj")

    assert graph.query("MATCH (:Function {name:'helper'})-[:CALLS]->() RETURN count(*)").result_set == [[0]]
    assert graph.query("MATCH (e {id:'extern://c/undefined_external_fn'}) RETURN count(e)").result_set == [[0]]


def test_ingest_repository_owns_c(graph, tmp_path):
    origin = tmp_path / "shapes_app"
    shutil.copytree(_SAMPLE, origin)
    repo = git.Repo.init(str(origin), initial_branch="main")
    with repo.config_writer() as cw:
        cw.set_value("user", "email", "t@e.com")
        cw.set_value("user", "name", "T")
    repo.git.add(A=True)
    commit = repo.index.commit("init").hexsha

    report = ingest.ingest_repository(GraphWriter(graph), str(origin), ref=commit, project_name="shapes")

    assert "c" in [r.language for r in report.languages if r.result is not None]
    assert graph.query("MATCH (:Function {name:'shape_area'})-[:CALLS]->(:Function {name:'square'}) RETURN 1").result_set == [[1]]
