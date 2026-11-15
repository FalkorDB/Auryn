"""Unit tests for the project-wide Swift resolution pass (no FalkorDB): RawFile list -> graph
nodes/edges. Exercises top-level types/functions hanging off the Project, method/property
attachment (own body + extension members, incl. cross-file), the EXTENDS/IMPLEMENTS split
(superclass vs protocol conformance vs protocol inheritance), extension-added conformance,
enum members, DEPENDS_ON type references, CALLS resolution (self method, receiver-typed
property/param, construction -> initializer, free function), import placeholders, external-call
placeholders, and the ADR 0004 cross-repo linkage keys."""

from __future__ import annotations

from auryn.ingestors.swift import resolution
from auryn.ingestors.swift.file_parser import parse_source
from auryn.ingestors.swift.ids import project_id

_PROJ = project_id("demo")
# The root-level fixtures below (no Sources/ layout) belong to the implicit single module, named
# after the project. `tail` renders that module root as `~`, so the assertions read the same as
# before the module tier existed -- see test_swift_spm.py for the multi-target collision coverage.
_MODULE_ROOT = _PROJ + "/module/demo"


def _resolve(files: dict[str, str]):
    raw = [parse_source(src.encode(), path) for path, src in files.items()]
    items = list(resolution.resolve(raw, _PROJ))
    nodes = [i for i in items if type(i).__name__ == "GraphNode"]
    edges = [i for i in items if type(i).__name__ == "GraphEdge"]
    return nodes, edges


def _edges(edges, rel):
    def tail(k):
        return (k["id"].replace(_MODULE_ROOT, "~").replace(_PROJ + "/", "").replace(_PROJ, "~"))
    return {(tail(e.from_key), tail(e.to_key)): (e.properties or {}) for e in edges if e.rel_type == rel}


def _nodes(nodes, label):
    return {n.properties.get("name"): n for n in nodes if n.label == label}


def test_top_level_types_and_functions_hang_off_module():
    nodes, edges = _resolve({"F.swift": "class C {}\nstruct S {}\nenum E { case a }\nprotocol P {}\nfunc f() {}"})
    # The implicit single module (named after the project) holds the top-level items; `~` is it.
    assert _nodes(nodes, "Module")["demo"].key["id"] == _MODULE_ROOT
    assert ("swift://demo", _MODULE_ROOT) in {(e.from_key["id"], e.to_key["id"])
                                              for e in edges if e.rel_type == "HAS_MODULE"}
    has_type = _edges(edges, "HAS_TYPE")
    assert ("~", "~.class.c") in has_type
    assert ("~", "~.struct.s") in has_type
    assert ("~", "~.enum.e") in has_type
    assert ("~", "~.protocol.p") in has_type
    assert ("~", "~.func.f()") in _edges(edges, "HAS_FUNCTION")
    # DEFINED_IN links each type to its file
    assert any(rel[0] == "~.class.c" for rel in _edges(edges, "DEFINED_IN"))


def test_methods_and_properties_attached():
    nodes, edges = _resolve({
        "F.swift": (
            "struct Point {\n"
            "  var x: Double\n"
            "  var area: Double { return x }\n"
            "  func describe() -> String { return \"p\" }\n"
            "  init(x: Double) { self.x = x }\n"
            "}\n"
        ),
    })
    has_method = _edges(edges, "HAS_METHOD")
    assert ("~.struct.point", "~.struct.point.method.describe()") in has_method
    assert ("~.struct.point", "~.struct.point.method.init(double)") in has_method
    has_prop = _edges(edges, "HAS_PROPERTY")
    assert ("~.struct.point", "~.struct.point.property.x") in has_prop
    assert ("~.struct.point", "~.struct.point.property.area") in has_prop
    assert _nodes(nodes, "Property")["area"].properties["is_computed"] is True


def test_superclass_extends_and_protocol_implements():
    _, edges = _resolve({
        "F.swift": (
            "class Animal {}\n"
            "protocol Named {}\n"
            "class Dog: Animal, Named {}\n"
            "struct Robot: Named {}\n"
        ),
    })
    ext = _edges(edges, "EXTENDS")
    impl = _edges(edges, "IMPLEMENTS")
    assert ("~.class.dog", "~.class.animal") in ext        # superclass -> EXTENDS
    assert ("~.class.dog", "~.protocol.named") in impl     # conformance -> IMPLEMENTS
    assert ("~.struct.robot", "~.protocol.named") in impl  # struct conformance -> IMPLEMENTS


def test_protocol_inheritance_is_extends():
    _, edges = _resolve({"F.swift": "protocol Named {}\nprotocol Shape: Named {}"})
    assert ("~.protocol.shape", "~.protocol.named") in _edges(edges, "EXTENDS")


def test_extension_members_attached_and_conformance():
    # An extension in another file attaches its members to the project type, and adds a conformance.
    _, edges = _resolve({
        "a.swift": "struct Point { var x: Double }\nprotocol Named {}",
        "b.swift": "extension Point: Named {\n  var name: String { return \"pt\" }\n  func moved() -> Point { return self }\n}",
    })
    has_method = _edges(edges, "HAS_METHOD")
    has_prop = _edges(edges, "HAS_PROPERTY")
    assert ("~.struct.point", "~.struct.point.method.moved()") in has_method
    assert ("~.struct.point", "~.struct.point.property.name") in has_prop
    assert ("~.struct.point", "~.protocol.named") in _edges(edges, "IMPLEMENTS")


def test_enum_members():
    nodes, edges = _resolve({"F.swift": "enum Direction: String { case north, south }"})
    has_member = _edges(edges, "HAS_ENUM_MEMBER")
    assert ("~.enum.direction", "~.enum.direction.case.north") in has_member
    assert ("~.enum.direction", "~.enum.direction.case.south") in has_member


def test_calls_self_receiver_typed_construction_and_free():
    _, edges = _resolve({
        "F.swift": (
            "struct Store {\n"
            "  func put(_ v: Int) {}\n"
            "  func save(other: Store) {\n"
            "    other.put(1)\n"    # receiver-typed param -> Store.put
            "    self.put(2)\n"     # self.method -> Store.put (same target, deduped)
            "  }\n"
            "}\n"
            "func seed() -> Store { return build() }\n"   # free call -> build
            "func build() -> Store {\n"
            "  seed()\n"            # free call -> seed
            "  return seed()\n"
            "}\n"
        ),
    })
    calls = _edges(edges, "CALLS")
    assert ("~.struct.store.method.save(store)", "~.struct.store.method.put(int)") in calls
    assert ("~.func.build()", "~.func.seed()") in calls
    assert ("~.func.seed()", "~.func.build()") in calls
    assert all(p.get("resolution") == "name" for p in calls.values())


def test_implicit_self_bare_call_resolves_to_own_method():
    # Inside a method, an unqualified `describe()` is an implicit-self call to an own method.
    _, edges = _resolve({
        "F.swift": (
            "class Animal {\n"
            "  func speak() -> String { return describe() }\n"
            "  func describe() -> String { return \"a\" }\n"
            "}\n"
        ),
    })
    calls = _edges(edges, "CALLS")
    assert ("~.class.animal.method.speak()", "~.class.animal.method.describe()") in calls


def test_construction_resolves_to_initializer():
    _, edges = _resolve({
        "F.swift": (
            "struct Point { init(x: Int) {} }\n"
            "func make() -> Point { return Point(x: 0) }\n"
        ),
    })
    calls = _edges(edges, "CALLS")
    assert ("~.func.make()", "~.struct.point.method.init(int)") in calls


def test_imports_surface_as_module_placeholders():
    nodes, edges = _resolve({"F.swift": "import Foundation\nimport MyLib\nclass C {}"})
    mods = _nodes(nodes, "Module")
    assert mods["Foundation"].properties["origin"] == "stdlib"
    assert mods["Foundation"].properties["resolved"] is False
    assert mods["MyLib"].properties["origin"] == "external"
    imports = {(f, t) for (f, t) in _edges(edges, "IMPORTS")}
    assert ("file/F.swift", "extern://swift/module/Foundation") in imports
    assert ("file/F.swift", "extern://swift/module/MyLib") in imports


def test_external_method_call_is_placeholder():
    # A method call on a known stdlib type surfaces as a Method placeholder (ADR 0004).
    nodes, edges = _resolve({
        "F.swift": "struct W { var s: String\n  func describe() -> String { return s.uppercased() } }",
    })
    up = _nodes(nodes, "Method")["uppercased"]
    assert up.key["id"] == "extern://swift/String.uppercased"
    assert up.properties["resolved"] is False and up.properties["origin"] == "stdlib"
    edge = _edges(edges, "CALLS")[("~.struct.w.method.describe()", "extern://swift/String.uppercased")]
    assert edge["resolution"] == "external" and edge["receiver_type"] == "String"


def test_public_callables_carry_cross_repo_linkage_key():
    nodes, _ = _resolve({
        "F.swift": (
            "public func compute() {}\n"
            "func hidden() {}\n"
            "public struct S {\n"
            "  public func make() -> S { return self }\n"
            "}\n"
        ),
    })
    fns = _nodes(nodes, "Function")
    assert fns["compute"].properties["linkage_key"] == "extern://swift/demo.compute"
    assert "linkage_key" not in fns["hidden"].properties  # internal -> not externally visible
    assert _nodes(nodes, "Method")["make"].properties["linkage_key"] == "extern://swift/S.make"


def test_depends_on_param_return_and_property_types():
    _, edges = _resolve({
        "F.swift": (
            "struct Store {}\n"
            "struct Holder { var store: Store }\n"
            "func make(x: Store) -> Store { return x }\n"
        ),
    })
    dep = _edges(edges, "DEPENDS_ON")
    assert ("~.struct.holder.property.store", "~.struct.store") in dep
    assert ("~.func.make(store)", "~.struct.store") in dep


def test_doc_property_reaches_nodes():
    nodes, _ = _resolve({
        "F.swift": (
            "/// A store.\n"
            "public class Store {\n"
            "  /// Puts a value.\n"
            "  public func put() {}\n"
            "}\n"
            "/// A free helper.\n"
            "func helper() {}\n"
        ),
    })
    assert _nodes(nodes, "Class")["Store"].properties["doc"] == "A store."
    assert _nodes(nodes, "Method")["put"].properties["doc"] == "Puts a value."
    assert _nodes(nodes, "Function")["helper"].properties["doc"] == "A free helper."


def test_nested_types_use_has_nested_type():
    _, edges = _resolve({"F.swift": "struct Outer {\n  struct Inner {}\n}"})
    assert ("~.struct.outer", "~.struct.outer.struct.inner") in _edges(edges, "HAS_NESTED_TYPE")
