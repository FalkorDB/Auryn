"""Integration tests for Java ingestion wiring, against the real `graph` fixture. Proves the
ingestor is registered as the owner of `.java` files in the multi-language endpoint and that
the fast tree-sitter engine models the demo sample end-to-end: the package tier, class/
interface/enum/record extraction, EXTENDS/IMPLEMENTS resolution, and call resolution (receiver-
typed method, bare same-type, `new` -> constructor). Needs FalkorDB (the conftest fixture)."""

from __future__ import annotations

from pathlib import Path

from auryn.core import ingest
from auryn.core.graph_client import GraphWriter

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SAMPLE = _REPO_ROOT / "demo" / "java-ingestor" / "sample"


def _ingest(graph):
    ingest.ingest_directory(GraphWriter(graph), str(_SAMPLE), project_name="acme")

    def q(cypher):
        return graph.query(cypher).result_set

    return q


def test_ingest_directory_owns_java(graph):
    report = ingest.ingest_directory(GraphWriter(graph), str(_SAMPLE), project_name="acme")

    ingested = {r.language: r for r in report.languages if r.result is not None}
    assert "java" in ingested
    # Repository, Store, Status, Component, Widget, Point, Main
    assert ingested["java"].files_detected == 7
    assert not any(u["language"] == "java" for u in report.unsupported_detected)


def test_package_tier(graph):
    q = _ingest(graph)
    # The sample is a root-level `src/main/java/` layout (no subprojects), so the package prefix chain
    # roots at the Project and there is NO Module tier (that appears only with real subprojects --
    # see tests/ingestors/java/test_java_modules.py).
    assert q("MATCH (:Project)-[:HAS_MODULE]->(:Module) RETURN count(*)") == [[0]]
    assert q("MATCH (:Project)-[:HAS_PACKAGE]->(p:Package) RETURN p.name") == [["com"]]
    subs = {r[0] for r in q("MATCH (:Package {name:'acme'})-[:HAS_SUBPACKAGE]->(p) RETURN p.name")}
    assert subs == {"model", "app"}
    assert q("MATCH (p:Package {name:'model'})-[:HAS_TYPE]->(t) RETURN count(t)")[0][0] == 3


def test_type_kinds_and_members(graph):
    q = _ingest(graph)
    assert q("MATCH (c:Class {name:'Store'})-[:HAS_METHOD]->(m) RETURN count(m)")[0][0] == 2
    assert q("MATCH (i:Interface {name:'Repository'}) RETURN 1") == [[1]]
    assert q("MATCH (e:Enum {name:'Status'})-[:HAS_ENUM_MEMBER]->(m) RETURN count(m)")[0][0] == 2
    # record components become Parameters on the Record.
    assert sorted(r[0] for r in q("MATCH (:Record {name:'Point'})-[:HAS_PARAMETER]->(p) RETURN p.name")) == ["x", "y"]
    assert "store" in {r[0] for r in q("MATCH (:Class {name:'Widget'})-[:HAS_FIELD]->(f) RETURN f.name")}


def test_hierarchy_resolution(graph):
    q = _ingest(graph)
    assert q("MATCH (:Class {name:'Widget'})-[:EXTENDS]->(c:Class {name:'Component'}) RETURN 1") == [[1]]
    assert q("MATCH (:Class {name:'Store'})-[:IMPLEMENTS]->(i:Interface {name:'Repository'}) RETURN 1") == [[1]]


def test_call_resolution(graph):
    q = _ingest(graph)
    # receiver-typed: Widget.save -> Store.put (store is a field of type Store)
    assert q("MATCH (:Method {name:'save'})-[:CALLS]->(m:Method {name:'put'}) RETURN 1") == [[1]]
    # bare same-type: Widget.save -> Widget.render
    assert q("MATCH (:Method {name:'save'})-[:CALLS]->(m:Method {name:'render'}) RETURN 1") == [[1]]
    # `new Widget(...)` in Main.make -> Widget's constructor
    assert q(
        "MATCH (:Method {name:'make'})-[:CALLS]->(m:Method {name:'Widget', kind:'CONSTRUCTOR'}) RETURN 1"
    ) == [[1]]


def test_depends_on(graph):
    q = _ingest(graph)
    # Main.make(Store): both the parameter type and the return type resolve to project types.
    targets = {r[0] for r in q("MATCH (:Method {name:'make'})-[:DEPENDS_ON]->(t) RETURN t.name")}
    assert {"Store", "Widget"} <= targets


def test_reingest_is_idempotent(graph):
    q = _ingest(graph)
    before = q("MATCH (n) RETURN count(n)")[0][0]
    ingest.ingest_directory(GraphWriter(graph), str(_SAMPLE), project_name="acme")
    after = q("MATCH (n) RETURN count(n)")[0][0]
    assert before == after
