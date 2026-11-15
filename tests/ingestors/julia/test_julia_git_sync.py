"""Integration tests for Julia ingestion wiring, against the real `graph` fixture. Proves the
ingestor is registered as the owner of `.jl` files in the multi-language endpoint and that the
fast tree-sitter engine models the demo sample end-to-end: the module tier (incl. a nested
module), struct/abstract/primitive type extraction, the generic-function/method split, EXTENDS
(subtyping), DISPATCHES_ON (multiple dispatch), name-based CALLS, and DEPENDS_ON. Needs FalkorDB
(the conftest fixture)."""

from __future__ import annotations

from pathlib import Path

from auryn.core import ingest
from auryn.core.graph_client import GraphWriter

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SAMPLE = _REPO_ROOT / "demo" / "julia-ingestor" / "sample"


def _ingest(graph):
    ingest.ingest_directory(GraphWriter(graph), str(_SAMPLE), project_name="acme")

    def q(cypher):
        return graph.query(cypher).result_set

    return q


def test_ingest_directory_owns_julia(graph):
    report = ingest.ingest_directory(GraphWriter(graph), str(_SAMPLE), project_name="acme")

    ingested = {r.language: r for r in report.languages if r.result is not None}
    assert "julia" in ingested
    assert ingested["julia"].files_detected == 2  # shapes.jl, app.jl
    assert not any(u["language"] == "julia" for u in report.unsupported_detected)


def test_module_tier(graph):
    q = _ingest(graph)
    top = {r[0] for r in q("MATCH (:Project)-[:HAS_MODULE]->(m:Module) RETURN m.name")}
    assert top == {"Shapes", "App"}
    subs = {r[0] for r in q("MATCH (:Module {name:'Shapes'})-[:HAS_SUBMODULE]->(m) RETURN m.name")}
    assert subs == {"Geometry"}


def test_type_kinds_and_members(graph):
    q = _ingest(graph)
    assert q("MATCH (a:AbstractType {name:'Shape'}) RETURN 1") == [[1]]
    assert q("MATCH (s:Struct {name:'Canvas'}) RETURN s.is_mutable") == [[True]]
    assert q("MATCH (p:PrimitiveType {name:'Radian'}) RETURN p.bits") == [[64]]
    fields = {r[0] for r in q("MATCH (:Struct {name:'Canvas'})-[:HAS_FIELD]->(f) RETURN f.name")}
    assert fields == {"shapes", "background"}


def test_generic_function_and_methods(graph):
    q = _ingest(graph)
    # ONE generic function `area`, TWO methods (dispatching on Circle vs Rectangle).
    assert q("MATCH (f:Function {name:'area'})-[:HAS_METHOD]->(m) RETURN count(m)")[0][0] == 2
    assert q("MATCH (:Module {name:'Shapes'})-[:HAS_FUNCTION]->(:Function {name:'area'}) RETURN 1") == [[1]]


def test_hierarchy_and_dispatch(graph):
    q = _ingest(graph)
    assert q("MATCH (:Struct {name:'Circle'})-[:EXTENDS]->(:AbstractType {name:'Shape'}) RETURN 1") == [[1]]
    assert q("MATCH (:Struct {name:'Rectangle'})-[:EXTENDS]->(:AbstractType {name:'Shape'}) RETURN 1") == [[1]]
    # a method of `area` dispatches on Circle.
    assert q(
        "MATCH (m:Method {name:'area'})-[:DISPATCHES_ON]->(:Struct {name:'Circle'}) RETURN 1"
    ) == [[1]]


def test_call_resolution(graph):
    q = _ingest(graph)
    # same-scope: total_area -> area
    assert q("MATCH (:Method {name:'total_area'})-[:CALLS]->(:Function {name:'area'}) RETURN 1") == [[1]]
    # cross-module (name-based): App.report -> Shapes.total_area, App.demo -> Shapes.area
    assert q("MATCH (:Method {name:'report'})-[:CALLS]->(:Function {name:'total_area'}) RETURN 1") == [[1]]
    assert q("MATCH (:Method {name:'demo'})-[c:CALLS]->(:Function {name:'area'}) RETURN c.resolution") == [["name"]]


def test_depends_on(graph):
    q = _ingest(graph)
    # Canvas.shapes is `Vector{Shape}` -> the field depends on the project AbstractType Shape.
    assert q(
        "MATCH (:Field {name:'shapes'})-[:DEPENDS_ON]->(:AbstractType {name:'Shape'}) RETURN 1"
    ) == [[1]]


def test_reingest_is_idempotent(graph):
    q = _ingest(graph)
    before = q("MATCH (n) RETURN count(n)")[0][0]
    ingest.ingest_directory(GraphWriter(graph), str(_SAMPLE), project_name="acme")
    after = q("MATCH (n) RETURN count(n)")[0][0]
    assert before == after
