"""Unit tests for the Java tree-sitter parser (no FalkorDB). Confirms the empirically-derived
grammar handling: package/imports (incl. static + wildcard), class heritage (single superclass
vs interface list), modifiers + annotations as anon/named children, fields (incl. multi-
declarator), methods (return type, params, varargs, throws), constructors, enums, records with
components, nested types, and call/`new` extraction with receivers."""

from __future__ import annotations

from auryn.ingestors.java.file_parser import parse_source


def _parse(src: str, path: str = "T.java"):
    return parse_source(src.encode(), path)


def test_package_and_imports():
    rf = _parse(
        """
        package com.acme.app;
        import java.util.List;
        import static java.lang.Math.max;
        import com.acme.other.*;
        class T {}
        """
    )
    assert rf.package == "com.acme.app"
    by_name = {i.name: i for i in rf.imports}
    assert by_name["java.util.List"].is_static is False and by_name["java.util.List"].is_wildcard is False
    assert by_name["java.lang.Math.max"].is_static is True
    assert by_name["com.acme.other"].is_wildcard is True


def test_class_heritage_modifiers_annotations():
    rf = _parse(
        """
        package p;
        @Service
        public abstract class Widget<T> extends Base<T> implements Named, Comparable<T> {
        }
        """
    )
    (c,) = rf.types
    assert c.kind == "Class" and c.name == "Widget"
    assert c.accessibility == "public" and set(c.modifiers) >= {"public", "abstract"}
    assert c.type_parameters == ["T"]
    assert c.superclass == "Base"  # generic args stripped
    assert c.interfaces == ["Named", "Comparable"]
    assert [a.name for a in c.annotations] == ["Service"]


def test_fields_including_multi_declarator():
    rf = _parse(
        """
        class C {
          private final Store store;
          public static final int MAX = 10;
          int a, b;
        }
        """
    )
    (c,) = rf.types
    fields = {f.name: f for f in c.fields}
    assert set(fields) == {"store", "MAX", "a", "b"}  # `int a, b;` -> two fields
    assert fields["store"].accessibility == "private" and "final" in fields["store"].modifiers
    assert fields["MAX"].type == "int" and "static" in fields["MAX"].modifiers
    assert fields["a"].accessibility == "package"  # no modifier -> package-private


def test_methods_params_varargs_throws_and_constructor():
    rf = _parse(
        """
        class C {
          public C(Store store) { }
          public List<String> f(Map<String,Integer> m, int... rest) throws IOException { return null; }
          @Override
          void g() {}
        }
        """
    )
    (c,) = rf.types
    methods = {m.name: m for m in c.methods}
    assert methods["C"].kind == "CONSTRUCTOR" and methods["C"].return_type is None
    f = methods["f"]
    assert f.return_type == "List<String>"
    assert [(p.name, p.is_varargs) for p in f.parameters] == [("m", False), ("rest", True)]
    assert f.throws == ["IOException"]
    assert [a.name for a in methods["g"].annotations] == ["Override"]


def test_interface_enum_record():
    rf = _parse(
        """
        package p;
        interface Named extends Base { void greet(String who); default int size() { return 0; } }
        enum Color implements Named { RED, GREEN, BLUE; }
        record Point(int x, int y) implements Shape { int sum() { return x + y; } }
        """
    )
    by_name = {t.name: t for t in rf.types}
    assert by_name["Named"].kind == "Interface" and by_name["Named"].interfaces == ["Base"]
    assert {m.name for m in by_name["Named"].methods} == {"greet", "size"}
    assert by_name["Color"].kind == "Enum"
    assert [m.name for m in by_name["Color"].enum_members] == ["RED", "GREEN", "BLUE"]
    assert by_name["Color"].interfaces == ["Named"]
    pt = by_name["Point"]
    assert pt.kind == "Record"
    assert [(p.name, p.type) for p in pt.record_components] == [("x", "int"), ("y", "int")]
    assert pt.interfaces == ["Shape"]


def test_nested_types():
    rf = _parse("class Outer { class Inner {} static class SNested {} }")
    (outer,) = rf.types
    assert {t.name for t in outer.nested_types} == {"Inner", "SNested"}


def test_call_extraction_receiver_and_new():
    rf = _parse(
        """
        class C {
          void m(Store store) {
            foo();
            this.bar();
            store.put();
            Helper.stat();
            new Widget();
          }
        }
        """
    )
    (c,) = rf.types
    (m,) = c.methods
    calls = {(x.name, x.receiver, x.is_new) for x in m.calls}
    assert ("foo", None, False) in calls
    assert ("bar", "this", False) in calls
    assert ("put", "store", False) in calls
    assert ("stat", "Helper", False) in calls
    assert ("Widget", None, True) in calls


# -- doc comments (shared `doc` property via core.docstrings.leading_comment) ---------------


def test_class_doc_javadoc_block():
    rf = _parse(
        """
        package p;
        /**
         * Store persists widgets.
         * @author acme
         */
        public class Store {}
        """
    )
    (c,) = rf.types
    assert c.doc == "Store persists widgets.\n@author acme"


def test_method_and_constructor_doc():
    rf = _parse(
        """
        class Store {
          // Builds an empty Store.
          public Store() {}
          /** Puts a value. */
          public void put(String v) {}
          public void undocumented() {}
        }
        """
    )
    methods = {m.name: m for m in rf.types[0].methods}
    assert methods["Store"].doc == "Builds an empty Store."   # constructor, single-line //
    assert methods["put"].doc == "Puts a value."              # ordinary method, Javadoc
    assert methods["undocumented"].doc == ""


def test_doc_survives_annotation_between_comment_and_method():
    # The Javadoc sits above the method's modifiers/annotations, still a preceding sibling.
    rf = _parse(
        """
        class C {
          /** Overridden save. */
          @Override
          public void save() {}
        }
        """
    )
    (save,) = rf.types[0].methods
    assert save.doc == "Overridden save."


def test_interface_and_nested_type_doc():
    rf = _parse(
        """
        package p;
        /** A thing. */
        interface Thing {
          /** Gets it. */
          String get();
        }
        class Outer {
          /** An inner. */
          static class Inner {}
        }
        """
    )
    by_name = {t.name: t for t in rf.types}
    assert by_name["Thing"].doc == "A thing."
    (get,) = by_name["Thing"].methods
    assert get.doc == "Gets it."
    (inner,) = by_name["Outer"].nested_types
    assert inner.doc == "An inner."


def test_blank_line_between_comment_and_decl_is_not_doc():
    rf = _parse(
        """
        package p;
        // This is a stray comment, not attached.

        public class Loose {}
        """
    )
    (loose,) = rf.types
    assert loose.doc == ""
