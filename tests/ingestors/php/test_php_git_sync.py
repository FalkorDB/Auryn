"""Integration tests for PHP ingestion wiring, against the real `graph` fixture. Proves the
ingestor is registered as the owner of `.php` files in the multi-language endpoint and that the
fast tree-sitter engine models the demo sample end-to-end: the namespace tier, class/interface/
trait/enum/function extraction, EXTENDS/IMPLEMENTS/USES_TRAIT resolution, constructor property
promotion (a promoted param becomes both a Parameter and a Property), and call resolution
(receiver-typed method, `$this` same-type, `new` -> constructor, bare function call). Needs
FalkorDB (the conftest fixture)."""

from __future__ import annotations

from pathlib import Path

from auryn.core import ingest
from auryn.core.graph_client import GraphWriter

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SAMPLE = _REPO_ROOT / "demo" / "php-ingestor" / "sample"


def _ingest(graph):
    ingest.ingest_directory(GraphWriter(graph), str(_SAMPLE), project_name="acme")

    def q(cypher):
        return graph.query(cypher).result_set

    return q


def test_ingest_directory_owns_php(graph):
    report = ingest.ingest_directory(GraphWriter(graph), str(_SAMPLE), project_name="acme")

    ingested = {r.language: r for r in report.languages if r.result is not None}
    assert "php" in ingested
    # src/helpers.php + Models/{AbstractStore,Logger,Repository,Status,Store,Timestamps}
    # + Web/{Component,Renderable,IdentifiableTrait,Widget,Main,helpers}
    assert ingested["php"].files_detected == 13
    assert not any(u["language"] == "php" for u in report.unsupported_detected)


def test_namespace_tier(graph):
    q = _ingest(graph)
    assert q("MATCH (:Project)-[:HAS_NAMESPACE]->(n:Namespace) RETURN n.name") == [["App"]]
    subs = {r[0] for r in q("MATCH (:Namespace {name:'App'})-[:HAS_SUBNAMESPACE]->(n) RETURN n.name")}
    assert subs == {"Models", "Web"}
    assert q("MATCH (n:Namespace {name:'Models'})-[:HAS_TYPE]->(t) RETURN count(t)")[0][0] == 6
    assert q("MATCH (n:Namespace {name:'Web'})-[:HAS_TYPE]->(t) RETURN count(t)")[0][0] == 5


def test_type_kinds_and_members(graph):
    q = _ingest(graph)
    assert q("MATCH (i:Interface {name:'Repository'}) RETURN 1") == [[1]]
    assert q("MATCH (t:Trait {name:'Timestamps'}) RETURN 1") == [[1]]
    assert q("MATCH (e:Enum {name:'Status'})-[:HAS_ENUM_CASE]->(c) RETURN count(c)")[0][0] == 2
    assert q("MATCH (:Class {name:'Store'})-[:HAS_CONSTANT]->(k) RETURN k.name") == [["MAX_ITEMS"]]
    assert q("MATCH (:Class {name:'Store'})-[:HAS_METHOD]->(m) RETURN count(m)")[0][0] >= 3


def test_constructor_property_promotion(graph):
    q = _ingest(graph)
    # Store::__construct(private Logger $logger): the promoted param becomes a Property ...
    assert q(
        "MATCH (:Class {name:'Store'})-[:HAS_PROPERTY]->(p:Property {name:'logger', is_promoted:true}) RETURN 1"
    ) == [[1]]
    # ... AND is still a Parameter on the constructor.
    assert q(
        "MATCH (:Class {name:'Store'})-[:HAS_METHOD]->(:Method {kind:'CONSTRUCTOR'})"
        "-[:HAS_PARAMETER]->(:Parameter {name:'logger'}) RETURN 1"
    ) == [[1]]


def test_hierarchy_resolution(graph):
    q = _ingest(graph)
    assert q("MATCH (:Class {name:'Store'})-[:EXTENDS]->(:Class {name:'AbstractStore'}) RETURN 1") == [[1]]
    assert q("MATCH (:Class {name:'Store'})-[:IMPLEMENTS]->(:Interface {name:'Repository'}) RETURN 1") == [[1]]
    assert q("MATCH (:Class {name:'Store'})-[:USES_TRAIT]->(:Trait {name:'Timestamps'}) RETURN 1") == [[1]]
    assert q("MATCH (:Class {name:'Widget'})-[:EXTENDS]->(:Class {name:'Component'}) RETURN 1") == [[1]]
    assert q("MATCH (:Class {name:'Widget'})-[:IMPLEMENTS]->(:Interface {name:'Renderable'}) RETURN 1") == [[1]]


def test_call_resolution(graph):
    q = _ingest(graph)
    # receiver-typed: Widget.save -> Store.put ($this->store is typed Store)
    assert q("MATCH (:Method {name:'save'})-[:CALLS]->(:Method {name:'put'}) RETURN 1") == [[1]]
    # `$this` same-type: Widget.save -> Widget.render
    assert q("MATCH (:Method {name:'save'})-[:CALLS]->(:Method {name:'render'}) RETURN 1") == [[1]]
    # `new Store(...)` in make_widget -> Store's constructor
    assert q(
        "MATCH (:Function {name:'make_widget'})-[:CALLS]->(:Method {kind:'CONSTRUCTOR'})"
        "<-[:HAS_METHOD]-(:Class {name:'Store'}) RETURN 1"
    ) == [[1]]
    # bare function call: bootstrap() -> version()
    assert q("MATCH (:Function {name:'bootstrap'})-[:CALLS]->(:Function {name:'version'}) RETURN 1") == [[1]]


def test_depends_on(graph):
    q = _ingest(graph)
    # Widget's `store` property (a promoted ctor param, typed Store) depends on Store.
    store_deps = {r[0] for r in q(
        "MATCH (:Class {name:'Widget'})-[:HAS_PROPERTY]->(:Property {name:'store'})-[:DEPENDS_ON]->(t) RETURN t.name"
    )}
    assert "Store" in store_deps
    # make_widget(Logger): Logger)-> Widget: both the parameter and return types resolve.
    fn_deps = {r[0] for r in q("MATCH (:Function {name:'make_widget'})-[:DEPENDS_ON]->(t) RETURN t.name")}
    assert {"Logger", "Widget"} <= fn_deps


def test_free_functions_hang_off_namespace(graph):
    q = _ingest(graph)
    funcs = {r[0] for r in q("MATCH (:Namespace {name:'App'})-[:HAS_FUNCTION]->(f) RETURN f.name")}
    assert {"bootstrap", "version"} <= funcs


def test_reingest_is_idempotent(graph):
    q = _ingest(graph)
    before = q("MATCH (n) RETURN count(n)")[0][0]
    ingest.ingest_directory(GraphWriter(graph), str(_SAMPLE), project_name="acme")
    after = q("MATCH (n) RETURN count(n)")[0][0]
    assert before == after
