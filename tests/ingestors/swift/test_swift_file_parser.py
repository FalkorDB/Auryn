"""Unit tests for the Swift tree-sitter parser (no FalkorDB). Confirms the empirically-derived
grammar handling (tree-sitter-swift 0.7.3): imports; the four type kinds (class/struct/enum are
one `class_declaration` discriminated by `declaration_kind`, protocol is `protocol_declaration`);
inheritance clauses; functions/methods with Swift argument labels (`to name:`, `_ count:`);
stored vs computed properties (var/let/static); initializers; enum cases (multi-per-line +
associated/raw values); extensions (members + added conformances); call extraction of every
shape (bare / method / self / static / construction / chain); and doc comments."""

from __future__ import annotations

from auryn.ingestors.swift.file_parser import parse_source


def _parse(src: str, path: str = "T.swift"):
    return parse_source(src.encode(), path)


def test_imports():
    rf = _parse("import Foundation\nimport UIKit\nimport struct Combine.Just\n")
    assert [i.name for i in rf.imports] == ["Foundation", "UIKit", "Combine"]


def test_type_kinds_and_inheritance():
    rf = _parse(
        """
        class Animal {}
        class Dog: Animal, Named {}
        struct Point: Shape {}
        enum Direction: String { case north }
        protocol Shape: Named {}
        """
    )
    by_name = {t.name: t for t in rf.types}
    assert by_name["Animal"].kind == "Class"
    assert by_name["Dog"].kind == "Class" and by_name["Dog"].inherits == ["Animal", "Named"]
    assert by_name["Point"].kind == "Struct" and by_name["Point"].inherits == ["Shape"]
    assert by_name["Direction"].kind == "Enum" and by_name["Direction"].inherits == ["String"]
    assert by_name["Shape"].kind == "Interface" and by_name["Shape"].inherits == ["Named"]


def test_functions_and_argument_labels():
    rf = _parse(
        """
        func greet(to name: String, _ count: Int, plain: Bool) -> [Int] {
            return []
        }
        func noReturn() {}
        """
    )
    by_name = {f.name: f for f in rf.functions}
    greet = by_name["greet"]
    assert greet.kind == "FUNCTION" and greet.return_type == "[Int]"
    labelled = [(p.name, p.label, p.type) for p in greet.parameters]
    assert labelled == [("name", "to", "String"), ("count", "", "Int"), ("plain", "plain", "Bool")]
    assert by_name["noReturn"].return_type is None


def test_methods_and_static():
    rf = _parse(
        """
        struct Point {
            func describe() -> String { return "p" }
            static func origin() -> Point { return Point() }
        }
        """
    )
    (pt,) = rf.types
    methods = {m.name: m for m in pt.methods}
    assert methods["describe"].kind == "METHOD" and methods["describe"].is_static is False
    assert methods["origin"].is_static is True and methods["origin"].return_type == "Point"


def test_properties_stored_computed_static_access():
    rf = _parse(
        """
        class C {
            var legs: Int = 4
            let species: String
            var area: Double { return 1.0 }
            static let shared = C()
            private var hidden: [String: Int] = [:]
        }
        """
    )
    (c,) = rf.types
    props = {p.name: p for p in c.properties}
    assert props["legs"].type == "Int" and props["legs"].is_constant is False and props["legs"].is_computed is False
    assert props["species"].type == "String" and props["species"].is_constant is True
    assert props["area"].is_computed is True and props["area"].type == "Double"
    assert props["shared"].is_static is True and props["shared"].is_constant is True
    assert props["hidden"].accessibility == "private" and props["hidden"].type == "[String: Int]"


def test_initializers():
    rf = _parse(
        """
        class C {
            init(species: String) {}
            convenience init() {}
        }
        """
    )
    (c,) = rf.types
    inits = [m for m in c.methods if m.name == "init"]
    assert len(inits) == 2
    assert all(m.kind == "INITIALIZER" and m.return_type is None for m in inits)
    assert any([p.name for p in m.parameters] == ["species"] for m in inits)
    assert any(m.parameters == [] for m in inits)


def test_enum_cases_multi_associated_raw():
    rf = _parse(
        """
        enum E: String {
            case a, b
            case named(Int, label: String)
            case raw = "R"
        }
        """
    )
    (e,) = rf.types
    cases = {m.name: m for m in e.enum_members}
    assert set(cases) == {"a", "b", "named", "raw"}
    assert cases["named"].associated_values  # captured the associated value payload
    assert cases["raw"].raw_value == '"R"'


def test_protocol_requirements():
    rf = _parse(
        """
        protocol Shape {
            var area: Double { get }
            func describe() -> String
        }
        """
    )
    (p,) = rf.types
    assert p.kind == "Interface"
    assert [m.name for m in p.methods] == ["describe"]
    assert p.methods[0].has_body is False
    assert [pr.name for pr in p.properties] == ["area"]


def test_extension_members_and_conformance():
    rf = _parse(
        """
        extension Point: Named {
            var name: String { return "pt" }
            func moved(by delta: Double) -> Point { return self }
        }
        """
    )
    assert rf.types == []
    (ext,) = rf.extensions
    assert ext.type_name == "Point" and ext.conformances == ["Named"]
    assert [m.name for m in ext.methods] == ["moved"]
    assert [p.name for p in ext.properties] == ["name"]
    # the argument label on the extension method's parameter is captured
    (delta,) = ext.methods[0].parameters
    assert (delta.name, delta.label, delta.type) == ("delta", "by", "Double")


def test_nested_types():
    rf = _parse(
        """
        struct Outer {
            struct Inner { var x: Int }
            enum Kind { case a }
        }
        """
    )
    (outer,) = rf.types
    assert outer.name == "Outer"
    nested = {t.name: t.kind for t in outer.nested_types}
    assert nested == {"Inner": "Struct", "Kind": "Enum"}


def test_call_extraction_shapes():
    rf = _parse(
        """
        func run(store: Store) {
            doThing()
            self.helper()
            store.put(1)
            Point(x: 0, y: 0)
            Logger.shared()
            a.b.deep()
        }
        """
    )
    (run,) = rf.functions
    calls = {(c.name, c.receiver) for c in run.calls}
    assert ("doThing", None) in calls          # bare call
    assert ("helper", "self") in calls          # self.method()
    assert ("put", "store") in calls            # receiver.method()
    assert ("Point", None) in calls             # construction (bare capitalized)
    assert ("shared", "Logger") in calls        # Type.static()
    assert ("deep", "b") in calls               # chain -> last segment before the call


def test_doc_comments():
    rf = _parse(
        """
        /// A drawable shape.
        /// Second line.
        class Shape {
            /// Computes the area.
            func area() -> Double { return 0 }
        }

        // not a doc for the next thing

        func loose() {}
        """
    )
    (shape,) = rf.types
    assert shape.doc == "A drawable shape.\nSecond line."
    assert shape.methods[0].doc == "Computes the area."
    assert next(f for f in rf.functions if f.name == "loose").doc == ""


def test_block_doc_comment():
    rf = _parse(
        """
        /** Widget is a drawable thing. */
        struct Widget {}
        """
    )
    (w,) = rf.types
    assert w.doc == "Widget is a drawable thing."
