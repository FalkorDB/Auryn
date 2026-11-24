"""Integration tests for Go ingestion wiring, against the real `graph` fixture. Proves the
ingestor is registered as the owner of `.go` files in the multi-language endpoint and that the
fast tree-sitter engine models the demo sample end-to-end: the flat package tier, struct/
interface/type-alias extraction, method attachment to receiver types, EMBEDS (struct + interface
embedding), DEPENDS_ON resolution (field/param/construction, incl. cross-package), and CALLS
resolution (bare same-package, receiver-typed, package-qualified cross-package). Needs FalkorDB
(the conftest fixture).

Ingests pin `deep="off"`: this suite asserts the FAST engine's name-based resolution (e.g. a CALLS
`resolution="name"`), which the deep scip-go pass would upgrade to `"resolved"` when scip-go is
installed -- so pinning it keeps these tests deterministic regardless of the host. The deep pass has
its own coverage in test_go_deep.py."""

from __future__ import annotations

from pathlib import Path

from auryn.core import ingest
from auryn.core.graph_client import GraphWriter

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SAMPLE = _REPO_ROOT / "demo" / "go-ingestor" / "sample"


def _ingest(graph):
    ingest.ingest_directory(GraphWriter(graph), str(_SAMPLE), project_name="acme", deep="off")

    def q(cypher):
        return graph.query(cypher).result_set

    return q


def test_ingest_directory_owns_go(graph):
    report = ingest.ingest_directory(GraphWriter(graph), str(_SAMPLE), project_name="acme", deep="off")

    ingested = {r.language: r for r in report.languages if r.result is not None}
    assert "go" in ingested
    # reader.go, repository.go, store.go, service.go, main.go
    assert ingested["go"].files_detected == 5
    assert not any(u["language"] == "go" for u in report.unsupported_detected)


def test_module_and_package_tier(graph):
    q = _ingest(graph)
    # One Module (the go.mod) over DIRECTORY-keyed Packages (not bare clause names). Packages hang
    # off their owning Module and carry their canonical import path.
    assert q("MATCH (:Project)-[:HAS_MODULE]->(m:Module) RETURN m.name") == [["example.com/acme/proj"]]
    pkgs = {(r[0], r[1]) for r in q("MATCH (:Module)-[:HAS_PACKAGE]->(p:Package) RETURN p.name, p.import_path")}
    assert pkgs == {("model", "example.com/acme/proj/model"), ("app", "example.com/acme/proj/app")}
    # Go package names are not hierarchical -> the ingestor emits no HAS_SUBPACKAGE edges.
    assert q("MATCH ()-[r:HAS_SUBPACKAGE]->() RETURN count(r)")[0][0] == 0
    # model owns Reader, Repository, ID, Base, Store.
    assert q("MATCH (p:Package {name:'model'})-[:HAS_TYPE]->(t) RETURN count(t)")[0][0] == 5


def test_type_kinds_and_members(graph):
    q = _ingest(graph)
    assert q("MATCH (i:Interface {name:'Repository'}) RETURN 1") == [[1]]
    assert q("MATCH (a:TypeAlias {name:'ID'}) RETURN 1") == [[1]]
    # Store has 5 concrete methods: Put, record, Size, Merge, Read.
    assert q("MATCH (:Struct {name:'Store'})-[:HAS_METHOD]->(m) RETURN count(m)")[0][0] == 5
    # embedded fields are NOT named Fields; Store's only named field is `items`.
    assert {r[0] for r in q("MATCH (:Struct {name:'Store'})-[:HAS_FIELD]->(f) RETURN f.name")} == {"items"}
    # interface method specs are Methods hung off the Interface.
    assert q("MATCH (:Interface {name:'Repository'})-[:HAS_METHOD]->(m) RETURN count(m)")[0][0] == 2
    # free functions hang off their package.
    assert q("MATCH (:Package {name:'model'})-[:HAS_FUNCTION]->(f {name:'New'}) RETURN 1") == [[1]]
    # consts.
    assert {r[0] for r in q("MATCH (:Package {name:'model'})-[:HAS_CONST]->(c) RETURN c.name")} == {"MaxItems", "Prefix", "Version"}


def test_embeds(graph):
    q = _ingest(graph)
    # struct embedding.
    assert q("MATCH (:Struct {name:'Store'})-[:EMBEDS]->(b:Struct {name:'Base'}) RETURN 1") == [[1]]
    # interface embedding.
    assert q("MATCH (:Interface {name:'Repository'})-[:EMBEDS]->(r:Interface {name:'Reader'}) RETURN 1") == [[1]]


def test_call_resolution(graph):
    q = _ingest(graph)
    # receiver-typed: Store.Put -> Store.record (via the receiver `s`).
    assert q("MATCH (:Method {name:'Put'})-[:CALLS]->(m:Method {name:'record'}) RETURN 1") == [[1]]
    # bare same-package: app.Run -> app.NewService.
    assert q("MATCH (:Function {name:'Run'})-[:CALLS]->(f:Function {name:'NewService'}) RETURN 1") == [[1]]
    # package-qualified cross-package: app.NewService -> model.New.
    assert q("MATCH (:Function {name:'NewService'})-[c:CALLS]->(f:Function {name:'New'}) RETURN c.resolution") == [["name"]]


def test_depends_on(graph):
    q = _ingest(graph)
    # construction / return type: model.New -> Store.
    assert q("MATCH (:Function {name:'New'})-[:DEPENDS_ON]->(s:Struct {name:'Store'}) RETURN 1") == [[1]]
    # cross-package field type: Service.store (*model.Store) -> model.Store.
    assert q("MATCH (:Field {name:'store'})-[:DEPENDS_ON]->(s:Struct {name:'Store'}) RETURN 1") == [[1]]


def test_no_implements_edge(graph):
    q = _ingest(graph)
    # Store structurally satisfies Repository, but IMPLEMENTS is deferred to the deep pass.
    assert q("MATCH ()-[r:IMPLEMENTS]->() RETURN count(r)")[0][0] == 0


def test_external_calls_surface_as_placeholders(graph):
    # ADR 0004: unresolved-but-nameable calls into imported non-project packages become first-class
    # placeholder nodes reached by ordinary CALLS edges, healable to a real def if that repo lands.
    q = _ingest(graph)
    # app.Run -> fmt.Println (imported stdlib) is no longer dropped: a placeholder with
    # resolved=false, origin=stdlib, keyed by its cross-repo linkage, on an `external` CALLS edge.
    assert q("MATCH (:Function {name:'Run'})-[c:CALLS]->(p {name:'Println'}) "
             "RETURN p.id, p.resolved, p.origin, c.resolution") == \
        [["extern://go/fmt.Println", False, "stdlib", "external"]]
    # model.Store.Put -> errors.New (imported stdlib) likewise surfaces.
    assert q("MATCH (:Method {name:'Put'})-[:CALLS]->(p {name:'New', resolved:false}) "
             "RETURN p.id, p.origin") == [["extern://go/errors.New", "stdlib"]]
    # real exported callables carry a project-independent cross-repo key -- now the IMPORT-PATH
    # qualified form a consumer actually writes (`<module>/<dir>.Name`), so a sibling repo importing
    # example.com/acme/proj/model heals onto it. (`resolved IS NULL` excludes the errors.New placeholder.)
    assert q("MATCH (f:Function {name:'New'}) WHERE f.resolved IS NULL "
             "RETURN f.linkage_key") == [["extern://go/example.com/acme/proj/model.New"]]
    # both the concrete Store.Put and the Repository.Put interface spec carry their own key.
    assert {r[0] for r in q("MATCH (m:Method {name:'Put'}) RETURN m.linkage_key")} == \
        {"extern://go/example.com/acme/proj/model.Store.Put", "extern://go/example.com/acme/proj/model.Repository.Put"}


def test_reingest_is_idempotent(graph):
    q = _ingest(graph)
    before = q("MATCH (n) RETURN count(n)")[0][0]
    ingest.ingest_directory(GraphWriter(graph), str(_SAMPLE), project_name="acme", deep="off")
    after = q("MATCH (n) RETURN count(n)")[0][0]
    assert before == after
