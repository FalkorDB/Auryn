"""Integration tests for Rust ingestion wiring, against the real `graph` fixture. Proves the
ingestor is registered as the owner of `.rs` files in the multi-language endpoint and that the
fast tree-sitter engine models the demo sample end-to-end: the module tier, struct/trait/enum
extraction, IMPLEMENTS/EXTENDS resolution, and call resolution (self method, receiver-typed
param, `Type::new()` associated fn, and free function). Needs FalkorDB (the conftest fixture)."""

from __future__ import annotations

from pathlib import Path

from auryn.core import ingest
from auryn.core.graph_client import GraphWriter

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SAMPLE = _REPO_ROOT / "demo" / "rust-ingestor" / "sample"


def _ingest(graph):
    ingest.ingest_directory(GraphWriter(graph), str(_SAMPLE), project_name="acme")

    def q(cypher):
        return graph.query(cypher).result_set

    return q


def test_ingest_directory_owns_rust(graph):
    report = ingest.ingest_directory(GraphWriter(graph), str(_SAMPLE), project_name="acme")

    ingested = {r.language: r for r in report.languages if r.result is not None}
    assert "rust" in ingested
    # src/lib.rs and src/app.rs
    assert ingested["rust"].files_detected == 2
    assert not any(u["language"] == "rust" for u in report.unsupported_detected)


def test_module_tier(graph):
    q = _ingest(graph)
    # Issue #5: crate-root modules hang off their Crate, which hangs off a Package under the Project.
    top = {r[0] for r in q("MATCH (:Crate)-[:HAS_MODULE]->(m:Module) RETURN m.name")}
    assert top == {"geometry", "app"}
    # The Cargo backbone exists: Project -> Package -> Crate -> Module.
    assert q("MATCH (:Project)-[:HAS_PACKAGE]->(:Package)-[:HAS_TARGET]->(c:Crate)"
             "-[:HAS_MODULE]->(:Module {name:'geometry'}) RETURN c.kind") == [["lib"]]
    # nested inline module -> HAS_SUBMODULE
    assert q("MATCH (:Module {name:'geometry'})-[:HAS_SUBMODULE]->(m:Module {name:'conversions'}) RETURN 1") == [[1]]
    # the inline `geometry` module owns its four types (Named, Shape, Circle, Kind).
    assert q("MATCH (:Module {name:'geometry'})-[:HAS_TYPE]->(t) RETURN count(t)")[0][0] == 4


def test_type_kinds_and_members(graph):
    q = _ingest(graph)
    assert q("MATCH (s:Struct {name:'Circle'})-[:HAS_FIELD]->(f) RETURN count(f)")[0][0] == 2
    assert q("MATCH (t:Interface {name:'Shape'}) RETURN 1") == [[1]]
    # enum with unit/tuple/struct variants
    kinds = {r[0] for r in q("MATCH (:Enum {name:'Kind'})-[:HAS_ENUM_MEMBER]->(v) RETURN v.kind")}
    assert kinds == {"unit", "tuple", "struct"}
    # associated function (no self) vs method (has self)
    assert q("MATCH (:Struct {name:'Circle'})-[:HAS_METHOD]->(m:Method {name:'new'}) RETURN m.is_associated") == [[True]]
    assert q("MATCH (:Struct {name:'Circle'})-[:HAS_METHOD]->(m:Method {name:'area'}) RETURN m.is_associated") == [[False]]
    # crate-level const (hangs off the Crate scope root, issue #5)
    assert q("MATCH (:Crate)-[:HAS_CONST]->(c:Const {name:'PI'}) RETURN 1") == [[1]]


def test_hierarchy_resolution(graph):
    q = _ingest(graph)
    # impl Trait for Type -> IMPLEMENTS (both a bare and a supertrait'd trait)
    assert q("MATCH (:Struct {name:'Circle'})-[:IMPLEMENTS]->(t:Interface {name:'Named'}) RETURN 1") == [[1]]
    assert q("MATCH (:Struct {name:'Circle'})-[:IMPLEMENTS]->(t:Interface {name:'Shape'}) RETURN 1") == [[1]]
    # supertrait bound -> EXTENDS
    assert q("MATCH (:Interface {name:'Shape'})-[:EXTENDS]->(t:Interface {name:'Named'}) RETURN 1") == [[1]]


def test_call_resolution(graph):
    q = _ingest(graph)
    # self.m(): Shape-impl Circle::area -> Circle::scaled_radius
    assert q("MATCH (:Method {name:'area'})-[:CALLS]->(m:Method {name:'scaled_radius'}) RETURN 1") == [[1]]
    # receiver-typed param: area_of(shape: &Circle) -> Circle::area
    assert q("MATCH (:Function {name:'area_of'})-[:CALLS]->(m:Method {name:'area'}) RETURN 1") == [[1]]
    # Type::new() associated fn: App::add_circle -> Circle::new (cross-file)
    assert q(
        "MATCH (:Method {name:'add_circle'})-[:CALLS]->(:Method {name:'new'})<-[:HAS_METHOD]-(:Struct {name:'Circle'}) RETURN 1"
    ) == [[1]]
    # free function call: run -> area_of
    assert q("MATCH (:Function {name:'run'})-[:CALLS]->(f:Function {name:'area_of'}) RETURN 1") == [[1]]
    # every locally-resolved CALLS edge carries the fast-engine name-based provenance; an
    # out-of-project call is stamped "external" instead (ADR 0004 placeholder, see below).
    assert {r[0] for r in q("MATCH ()-[c:CALLS]->() RETURN DISTINCT c.resolution")} == {"name", "external"}


def test_external_calls_surface_as_placeholders(graph):
    q = _ingest(graph)
    # Circle::new calls String::from -- an out-of-project path call -> a first-class placeholder
    # Function node keyed by its cross-repo linkage, reached by an ordinary (external) CALLS edge.
    assert q(
        "MATCH (:Method {name:'new'})-[c:CALLS {resolution:'external'}]->(p:Function {name:'from'}) "
        "RETURN p.id, p.resolved, p.origin, p.linkage_key"
    ) == [["extern://rust/String::from", False, "external", "extern://rust/String::from"]]
    # `self.label.clone()` -- a method call on a known external type (String) -> a Method placeholder.
    assert q(
        "MATCH (:Method {name:'name'})-[:CALLS {resolution:'external'}]->(p:Method {name:'clone'}) "
        "RETURN p.id, p.resolved, p.origin"
    ) == [["extern://rust/String::clone", False, "external"]]
    # real `pub` callables carry a cross-repo linkage_key; a method's is `Type::method`.
    assert q("MATCH (m:Method {name:'new'})<-[:HAS_METHOD]-(:Struct {name:'Circle'}) RETURN m.linkage_key") \
        == [["extern://rust/Circle::new"]]
    # a free function's is `crate::module::name`.
    assert q("MATCH (f:Function {name:'to_diameter'}) RETURN f.linkage_key") \
        == [["extern://rust/acme::geometry::conversions::to_diameter"]]


def test_depends_on(graph):
    q = _ingest(graph)
    # area_of(shape: &Circle) -> Circle both as a param and (return) type reference
    assert q("MATCH (:Function {name:'area_of'})-[:DEPENDS_ON]->(t:Struct {name:'Circle'}) RETURN 1") == [[1]]
    # total_area(app: &mut App, ...) -> App
    assert q("MATCH (:Function {name:'total_area'})-[:DEPENDS_ON]->(t:Struct {name:'App'}) RETURN 1") == [[1]]


def test_reingest_is_idempotent(graph):
    q = _ingest(graph)
    before = q("MATCH (n) RETURN count(n)")[0][0]
    ingest.ingest_directory(GraphWriter(graph), str(_SAMPLE), project_name="acme")
    after = q("MATCH (n) RETURN count(n)")[0][0]
    assert before == after
