"""Unit tests for the Kotlin tree-sitter parser (no FalkorDB). Confirms the empirically-derived
tree-sitter-kotlin 1.1.0 grammar handling: package/imports (incl. wildcard + alias), class
heritage (constructor-invocation superclass vs bare-user_type interface list), modifiers +
annotations as wrapper/named children, properties (val/var, incl. primary-ctor promoted), functions
(return type, params, varargs, defaults), primary + secondary constructors, enums, objects &
companion objects, nested types, top-level functions/properties, call/receiver extraction, and doc
comments."""

from __future__ import annotations

from auryn.ingestors.kotlin.file_parser import parse_source


def _parse(src: str, path: str = "T.kt"):
    return parse_source(src.encode(), path)


def test_package_and_imports():
    rf = _parse(
        """
        package com.acme.app
        import com.acme.model.Store
        import com.acme.other.*
        import com.acme.util.Helper as H
        class T
        """
    )
    assert rf.package == "com.acme.app"
    by_name = {i.name: i for i in rf.imports}
    assert by_name["com.acme.model.Store"].is_wildcard is False
    assert by_name["com.acme.model.Store"].alias is None
    assert by_name["com.acme.other"].is_wildcard is True
    assert by_name["com.acme.util.Helper"].alias == "H"


def test_class_heritage_modifiers_annotations():
    rf = _parse(
        """
        package p
        @Service
        public abstract class Widget<T>(val name: String, count: Int) : Base<T>(name), Named, Comparable<T> {
        }
        """
    )
    (c,) = rf.types
    assert c.kind == "Class" and c.name == "Widget"
    assert c.accessibility == "public" and set(c.modifiers) >= {"public", "abstract"}
    assert c.type_parameters == ["T"]
    assert c.superclass == "Base"  # the delegation specifier written `Base<T>(name)` -> superclass
    assert c.interfaces == ["Named", "Comparable"]  # bare specifiers -> interfaces
    assert [a.name for a in c.annotations] == ["Service"]
    # a `val` primary-ctor parameter is promoted to a property Field; a plain one is not.
    assert {f.name for f in c.fields} == {"name"}
    ctor = next(m for m in c.methods if m.kind == "CONSTRUCTOR")
    assert [p.name for p in ctor.parameters] == ["name", "count"]


def test_properties_val_var_and_accessibility():
    rf = _parse(
        """
        class C {
          private val store: Store
          var counter: Int = 0
          val label = "x"
        }
        """
    )
    (c,) = rf.types
    fields = {f.name: f for f in c.fields}
    assert set(fields) == {"store", "counter", "label"}
    assert fields["store"].accessibility == "private" and fields["store"].is_mutable is False
    assert fields["counter"].is_mutable is True and fields["counter"].type == "Int"
    assert fields["counter"].accessibility == "public"  # Kotlin default
    assert fields["label"].type == ""  # inferred type, none written


def test_functions_params_varargs_defaults_return_and_constructor():
    rf = _parse(
        """
        class C {
          constructor(store: Store) { }
          fun f(m: Map<String, Int>, b: String = "x", vararg rest: Int): List<String> { return g() }
          override fun g() {}
        }
        """
    )
    (c,) = rf.types
    methods = {m.name: m for m in c.methods}
    assert methods["C"].kind == "CONSTRUCTOR" and methods["C"].return_type is None
    assert [p.name for p in methods["C"].parameters] == ["store"]
    f = methods["f"]
    assert f.return_type == "List<String>"
    assert [(p.name, p.is_vararg, p.has_default) for p in f.parameters] == [
        ("m", False, False), ("b", False, True), ("rest", True, False),
    ]
    assert "override" in methods["g"].modifiers


def test_interface_enum_object():
    rf = _parse(
        """
        package p
        interface Named : Base {
          fun greet(who: String)
          fun size(): Int
          val label: String
        }
        enum class Color(val rgb: Int) : Named {
          RED(1), GREEN(2), BLUE(3);
          fun hex(): String = ""
        }
        object Singleton {
          fun instance(): Int = 42
        }
        """
    )
    by_name = {t.name: t for t in rf.types}
    assert by_name["Named"].kind == "Interface" and by_name["Named"].interfaces == ["Base"]
    assert {m.name for m in by_name["Named"].methods} == {"greet", "size"}
    assert {f.name for f in by_name["Named"].fields} == {"label"}
    color = by_name["Color"]
    assert color.kind == "Enum"
    assert [m.name for m in color.enum_members] == ["RED", "GREEN", "BLUE"]
    assert color.interfaces == ["Named"]
    assert {f.name for f in color.fields} == {"rgb"}  # promoted `val` enum-ctor property
    assert "hex" in {m.name for m in color.methods}
    sing = by_name["Singleton"]
    assert sing.kind == "Class" and sing.is_object is True
    assert {m.name for m in sing.methods} == {"instance"}


def test_companion_and_nested_types():
    rf = _parse(
        """
        class Outer {
          class Inner
          object Nested
          interface I
          companion object {
            fun create(): Int = 0
          }
        }
        """
    )
    (outer,) = rf.types
    nested = {t.name: t for t in outer.nested_types}
    assert set(nested) == {"Inner", "Nested", "I", "Companion"}
    assert nested["Inner"].kind == "Class"
    assert nested["Nested"].is_object is True
    assert nested["I"].kind == "Interface"
    assert nested["Companion"].is_companion is True and nested["Companion"].is_object is True
    assert {m.name for m in nested["Companion"].methods} == {"create"}


def test_call_extraction_receiver_and_constructor():
    rf = _parse(
        """
        class C {
          fun m(store: Store) {
            foo()
            this.bar()
            store.put()
            Helper.stat()
            Widget()
          }
        }
        """
    )
    (c,) = rf.types
    (m,) = c.methods
    calls = {(x.name, x.receiver) for x in m.calls}
    assert ("foo", None) in calls
    assert ("bar", "this") in calls
    assert ("put", "store") in calls
    assert ("stat", "Helper") in calls
    assert ("Widget", None) in calls  # Kotlin has no `new`; resolution decides it's a constructor


def test_top_level_functions_and_properties():
    rf = _parse(
        """
        package p
        val VERSION: String = "1.0"
        fun main(args: Array<String>) {
          greet()
        }
        fun greet() {}
        """
    )
    assert [p.name for p in rf.properties] == ["VERSION"]
    assert rf.properties[0].type == "String"
    funcs = {f.name: f for f in rf.functions}
    assert set(funcs) == {"main", "greet"}
    assert [p.name for p in funcs["main"].parameters] == ["args"]
    assert funcs["main"].parameters[0].type == "Array<String>"
    assert {(c.name, c.receiver) for c in funcs["main"].calls} == {("greet", None)}


# -- doc comments (shared `doc` property via core.docstrings.leading_comment) ---------------


def test_class_doc_kdoc_block():
    rf = _parse(
        """
        package p
        /**
         * Store persists widgets.
         * @author acme
         */
        class Store
        """
    )
    (c,) = rf.types
    assert c.doc == "Store persists widgets.\n@author acme"


def test_method_and_undocumented_doc():
    rf = _parse(
        """
        class Store {
          /** Puts a value. */
          fun put(v: String) {}
          fun undocumented() {}
        }
        """
    )
    methods = {m.name: m for m in rf.types[0].methods}
    assert methods["put"].doc == "Puts a value."
    assert methods["undocumented"].doc == ""


def test_doc_survives_annotation_between_comment_and_method():
    rf = _parse(
        """
        class C {
          /** Overridden save. */
          @Override
          fun save() {}
        }
        """
    )
    (save,) = rf.types[0].methods
    assert save.doc == "Overridden save."


def test_blank_line_between_comment_and_decl_is_not_doc():
    rf = _parse(
        """
        package p
        // This is a stray comment, not attached.

        class Loose
        """
    )
    (loose,) = rf.types
    assert loose.doc == ""
