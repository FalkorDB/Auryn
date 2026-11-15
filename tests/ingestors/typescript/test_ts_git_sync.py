"""Integration tests for TypeScript/JavaScript ingestion wiring, against the real `graph`
fixture. Proves the ingestor is registered as the owner of the JS/TS file family in the
multi-language endpoint and that the fast tree-sitter engine models the demo sample
end-to-end: one language spanning .ts/.js/.d.ts, the cross-module import graph (incl. a
cross-dialect .js import), heritage resolution, and call resolution (bare function, receiver-
typed method, `new` -> constructor). Needs FalkorDB (the conftest fixture)."""

from __future__ import annotations

import copy
from pathlib import Path

from auryn.core import ingest
from auryn.core.graph_client import GraphWriter
from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.ingestors.typescript import resolution
from auryn.ingestors.typescript.file_parser import parse_source

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SAMPLE = _REPO_ROOT / "demo" / "typescript-ingestor" / "sample"


def _ingest(graph):
    ingest.ingest_directory(GraphWriter(graph), str(_SAMPLE), project_name="widgets")

    def q(cypher):
        return graph.query(cypher).result_set

    return q


def test_ingest_directory_owns_the_js_ts_family(graph):
    report = ingest.ingest_directory(GraphWriter(graph), str(_SAMPLE), project_name="widgets")

    ingested = {r.language: r for r in report.languages if r.result is not None}
    assert "typescript" in ingested
    # base.ts, models.ts, widget.ts, index.ts, util.js, config.d.ts
    assert ingested["typescript"].files_detected == 6
    assert not any(u["language"] == "typescript" for u in report.unsupported_detected)


def test_modules_carry_dialect_and_declaration(graph):
    q = _ingest(graph)
    assert q("MATCH (m:Module {name:'util.js'}) RETURN m.dialect") == [["js"]]
    assert q("MATCH (m:Module {name:'models.ts'}) RETURN m.dialect") == [["ts"]]
    assert q("MATCH (m:Module {name:'config.d.ts'}) RETURN m.is_declaration") == [[True]]
    assert {r[0] for r in q("MATCH (:Project)-[:HAS_MODULE]->(m) RETURN m.name")} == {
        "base.ts", "models.ts", "widget.ts", "index.ts", "util.js", "config.d.ts",
    }


def test_types_members_and_enum(graph):
    q = _ingest(graph)
    assert sorted(r[0] for r in q("MATCH (:Class {name:'Store'})-[:HAS_METHOD]->(m) RETURN m.name")) == ["put", "size"]
    assert q("MATCH (e:Enum {name:'Status'}) RETURN e.members") == [[["Active", "Inactive"]]]
    assert q("MATCH (:Enum {name:'Status'})-[:HAS_ENUM_MEMBER]->(m) RETURN count(m)") == [[2]]
    # constructor parameter-property `store` becomes a Widget field.
    assert "store" in {r[0] for r in q("MATCH (:Class {name:'Widget'})-[:HAS_PROPERTY]->(p) RETURN p.name")}


def test_import_graph_including_cross_dialect(graph):
    q = _ingest(graph)
    imports = {(r[0], r[1]) for r in q("MATCH (a:Module)-[:IMPORTS]->(b:Module) RETURN a.name, b.name")}
    assert ("widget.ts", "base.ts") in imports
    assert ("widget.ts", "models.ts") in imports
    assert ("index.ts", "widget.ts") in imports
    assert ("index.ts", "util.js") in imports  # extensionless import resolves to the .js module


def test_heritage_resolution(graph):
    q = _ingest(graph)
    assert q("MATCH (:Class {name:'Widget'})-[:EXTENDS]->(c:Class) RETURN c.name") == [["Component"]]
    assert q("MATCH (:Class {name:'Widget'})-[:IMPLEMENTS]->(i:Interface) RETURN i.name") == [["Named"]]


def test_call_resolution(graph):
    q = _ingest(graph)
    # bare function call across the module: run -> greet (defined in util.js, a JS module).
    assert q("MATCH (:Function {name:'run'})-[:CALLS]->(g:Function {name:'greet'}) RETURN 1") == [[1]]
    assert q("MATCH (:Function {name:'run'})-[:CALLS]->(:Function {name:'make'}) RETURN 1") == [[1]]
    # receiver-typed method call: persist(store: Store) -> Store.put
    assert q("MATCH (:Function {name:'persist'})-[:CALLS]->(m:Method {name:'put'}) RETURN 1") == [[1]]
    # `new Widget(...)` -> Widget's constructor.
    assert q("MATCH (:Function {name:'make'})-[:CALLS]->(m:Method {name:'constructor'}) RETURN 1") == [[1]]


def test_reingest_is_idempotent(graph):
    q = _ingest(graph)
    before = q("MATCH (n) RETURN count(n)")[0][0]
    ingest.ingest_directory(GraphWriter(graph), str(_SAMPLE), project_name="widgets")
    after = q("MATCH (n) RETURN count(n)")[0][0]
    assert before == after  # content-hash short-circuit -> no duplicate nodes


# -- Streaming resolution (recommendation B): build_plan + emit_streaming must be a
#    byte-for-byte equivalent of resolve() over the whole file list. --

_B_CORPUS = {
    "a.ts": b"export class Base { greet(): string { return \"hi\"; } }",
    "b.ts": b'import { Base } from "./a"; export class Sub extends Base { run(): void { this.greet(); } }',
    "c.ts": b'import { Sub } from "./b"; import * as x from "lodash"; '
            b"export function main(): void { const s = new Sub(); s.run(); }",
    "d.ts": b"export interface Store { put(k: string): void; } export enum Color { Red, Green }",
    "e.ts": b"export type Id = string | number; export function id(x: Id): Id { return x; }",
}


def _multisets(items):
    nodes: dict = {}
    edges: dict = {}
    for it in items:
        if isinstance(it, GraphNode):
            k = (it.label, it.key["id"], tuple(sorted((p, repr(v)) for p, v in it.properties.items())))
            nodes[k] = nodes.get(k, 0) + 1
        else:
            k = (it.rel_type, it.from_key["id"], it.to_key["id"],
                 tuple(sorted((p, repr(v)) for p, v in it.properties.items())))
            edges[k] = edges.get(k, 0) + 1
    return nodes, edges


def test_emit_streaming_matches_resolve():
    raw = [parse_source(src, path) for path, src in _B_CORPUS.items()]
    proj = "ts://widgets"

    a_nodes, a_edges = _multisets(resolution.resolve([copy.deepcopy(f) for f in raw], proj))

    snapshot = {i: f for i, f in enumerate(raw)}
    refs = [(f.path, i) for i, f in enumerate(raw)]

    def load_fn(sha):  # fresh copy per call, as git_sync gets from the parse cache
        return copy.deepcopy(snapshot[sha])

    plan = resolution.build_plan(refs, load_fn, proj)
    b_nodes, b_edges = _multisets(list(resolution.emit_streaming(plan, load_fn, proj)))

    assert b_nodes == a_nodes
    assert b_edges == a_edges
    # sanity: the corpus exercised the cross-file cases, not an empty match
    assert any(rel == "EXTENDS" for (rel, _f, _t, _p) in a_edges)      # Sub extends Base (cross-file)
    assert any(rel == "CALLS" for (rel, _f, _t, _p) in a_edges)        # this.greet()/s.run()/new Sub()
    assert any(lbl == "Module" and "lodash" in nid for (lbl, nid, _p) in a_nodes)  # external placeholder
    assert plan.broken == []
