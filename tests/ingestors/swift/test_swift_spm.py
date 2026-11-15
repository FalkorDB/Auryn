"""SwiftPM module/target tier: packages are scoped by their SwiftPM target (Sources/<Target>/), so
two targets that both declare `Foo` stay distinct; the module carries a linkage_key so an
`import <Target>` heals onto the defining target (ADR 0004). Pure resolution tests need no FalkorDB;
the healing test uses the `graph` fixture."""

from __future__ import annotations

from auryn.core import ingest
from auryn.core.graph_client import GraphWriter
from auryn.ingestors.swift import resolution, swiftpm
from auryn.ingestors.swift.file_parser import parse_source
from auryn.ingestors.swift.ids import project_id


# -- pure: path-derived module -------------------------------------------------------------

def test_module_for_path():
    assert swiftpm.module_for_path("Sources/Billing/Charge.swift", "pkg") == "Billing"
    assert swiftpm.module_for_path("Sources/Billing/sub/X.swift", "pkg") == "Billing"
    assert swiftpm.module_for_path("Tests/BillingTests/T.swift", "pkg") == "BillingTests"
    assert swiftpm.module_for_path("Sources/main.swift", "pkg") == "pkg"   # flat single target
    assert swiftpm.module_for_path("main.swift", "pkg") == "pkg"           # no Sources/ layout


# -- pure: resolution ----------------------------------------------------------------------

def _resolve(files, project="demo"):
    proj = project_id(project)
    raw = [parse_source(src.encode(), path) for path, src in files.items()]
    items = list(resolution.resolve(raw, proj))
    nodes = [i for i in items if type(i).__name__ == "GraphNode"]
    edges = [i for i in items if type(i).__name__ == "GraphEdge"]
    return nodes, edges


def _pairs(edges, rel):
    return {(e.from_key["id"], e.to_key["id"]) for e in edges if e.rel_type == rel}


def test_two_targets_same_type_name_stay_distinct():
    # The bug: without a module tier, `struct Foo` in target A and target B collapse to one node.
    nodes, _ = _resolve({
        "Sources/A/F.swift": "public struct Foo {}",
        "Sources/B/F.swift": "public struct Foo {}",
    })
    assert sorted(n.key["id"] for n in nodes if n.label == "Struct") == \
        ["swift://demo/module/A.struct.foo", "swift://demo/module/B.struct.foo"]


def test_module_tier_and_containment():
    nodes, edges = _resolve({"Sources/Billing/C.swift": "struct Charge {}\nfunc bill() {}"})
    mod = next(n for n in nodes if n.label == "Module" and n.properties["name"] == "Billing")
    assert mod.key["id"] == "swift://demo/module/Billing"
    assert mod.properties["linkage_key"] == "extern://swift/module/Billing"
    assert ("swift://demo", "swift://demo/module/Billing") in _pairs(edges, "HAS_MODULE")
    assert ("swift://demo/module/Billing", "swift://demo/module/Billing.struct.charge") in _pairs(edges, "HAS_TYPE")
    assert ("swift://demo/module/Billing", "swift://demo/module/Billing.func.bill()") in _pairs(edges, "HAS_FUNCTION")


def test_refs_and_extensions_resolve_within_declaring_target():
    # Both targets declare `Foo`. B's field type and B's `extension Foo` must bind to B's Foo, not
    # A's -- the type-side analog of the free-function module preference (PR #8 review #2).
    _, edges = _resolve({
        "Sources/A/F.swift": "public struct Foo { public var a: Int }",
        "Sources/B/F.swift": ("public struct Foo { public var b: Int }\n"
                              "public struct Widget { var f: Foo }\n"
                              "extension Foo { public func tag() {} }"),
    })
    a_foo, b_foo = "swift://demo/module/A.struct.foo", "swift://demo/module/B.struct.foo"
    dep = _pairs(edges, "DEPENDS_ON")
    assert ("swift://demo/module/B.struct.widget.property.f", b_foo) in dep     # B's Widget.f -> B's Foo
    assert ("swift://demo/module/B.struct.widget.property.f", a_foo) not in dep
    hasm = _pairs(edges, "HAS_METHOD")
    assert (b_foo, b_foo + ".method.tag()") in hasm                             # extension attaches to B's Foo
    assert not any(src == a_foo and ".method.tag()" in dst for src, dst in hasm)


def test_extension_call_resolves_in_extension_module():
    # `extension Foo` is declared in B but Foo lives in A; an unqualified `helper()` inside it must
    # resolve to B's helper (the extension's module), not A's (the owner type's) (PR #8 review #4).
    _, edges = _resolve({
        "Sources/A/F.swift": "public struct Foo {}\npublic func helper() {}",
        "Sources/B/B.swift": "public func helper() {}\nextension Foo { public func run() { helper() } }",
    })
    run_id = "swift://demo/module/A.struct.foo.method.run()"  # attaches to A's Foo (B has none)
    calls = _pairs(edges, "CALLS")
    assert (run_id, "swift://demo/module/B.func.helper()") in calls            # decl module B wins
    assert (run_id, "swift://demo/module/A.func.helper()") not in calls


def test_hyphenated_target_name_is_c99_normalized():
    # SwiftPM imports Sources/My-Lib/ as module `My_Lib`; ids + linkage keys must use that form
    # or `import My_Lib` won't heal (PR #8 review #3).
    nodes, _ = _resolve({"Sources/My-Lib/F.swift": "public func go() {}"})
    mod = next(n for n in nodes if n.label == "Module")
    assert mod.properties["name"] == "My_Lib"
    assert mod.properties["linkage_key"] == "extern://swift/module/My_Lib"
    fn = next(n for n in nodes if n.label == "Function" and n.properties["name"] == "go")
    assert fn.key["id"] == "swift://demo/module/My_Lib.func.go()"
    assert fn.properties["linkage_key"] == "extern://swift/My_Lib.go"


def test_public_func_linkage_is_module_qualified():
    # A free function's cross-repo key is <module>.name -- its SwiftPM target, not the project.
    nodes, _ = _resolve({"Sources/Billing/charge.swift": "public func charge() {}"})
    fn = next(n for n in nodes if n.label == "Function" and n.properties["name"] == "charge")
    assert fn.properties["linkage_key"] == "extern://swift/Billing.charge"


# -- graph: cross-repo import heals onto the defining target -------------------------------

def _write(root, files):
    for rel, src in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    return root


def test_cross_repo_import_heals_onto_defining_target(graph, tmp_path):
    app = _write(tmp_path / "app", {"Sources/App/main.swift": "import Core\n\npublic func run() {}\n"})
    lib = _write(tmp_path / "lib", {"Sources/Core/core.swift": "public struct Thing {}\n"})
    w = GraphWriter(graph)

    ingest.ingest_directory(w, str(app), project_name="app")   # Core is an external import placeholder
    assert graph.query("MATCH (m:Module {id:'extern://swift/module/Core'}) "
                       "WHERE m.resolved = false RETURN 1").result_set == [[1]]

    ingest.ingest_directory(w, str(lib), project_name="lib")   # lib DEFINES target Core -> heal
    # app's import now points at lib's real Core module; the placeholder is gone.
    assert graph.query("MATCH (:File)-[:IMPORTS]->(m:Module {id:'swift://lib/module/Core'}) "
                       "RETURN 1").result_set == [[1]]
    assert graph.query("MATCH (m:Module {id:'extern://swift/module/Core'}) "
                       "RETURN count(m)").result_set == [[0]]
