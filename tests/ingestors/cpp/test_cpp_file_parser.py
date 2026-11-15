"""Unit tests for the C++ tree-sitter parser (no FalkorDB). Confirms the empirically-derived
grammar handling: preprocessor include/define, nested/anonymous namespaces, `enum class` vs
plain enum, struct/class/union, class heritage (base_class_clause with access specifiers),
member fields/methods (static/virtual/const), constructors/destructors, template classes,
free functions vs out-of-line `Owner::method` definitions (incl. nested `A::B::m`), call
extraction (bare/receiver/`Class::`/`new`), and -- critically -- that guarded header content
(inside `#ifndef ... #endif`) is not lost."""

from __future__ import annotations

from auryn.ingestors.cpp.file_parser import parse_source


def _parse(src: str, path: str = "t.cpp"):
    return parse_source(src.encode(), path)


def test_includes_and_macros():
    rf = _parse(
        """
        #include <vector>
        #include "geometry.hpp"
        #define MAX 100
        #define SQ(x) ((x)*(x))
        """
    )
    assert {(i.path, i.is_system) for i in rf.includes} == {("vector", True), ("geometry.hpp", False)}
    assert {(m.name, m.is_function_like) for m in rf.macros} == {("MAX", False), ("SQ", True)}


def test_nested_and_anonymous_namespaces():
    rf = _parse(
        """
        namespace a { namespace b { class Inner {}; } }
        namespace c::d { struct S {}; }
        namespace { int hidden_var; class Anon {}; }
        """
    )
    by_name = {t.name: t for t in rf.types}
    assert by_name["Inner"].namespace == "a::b"      # nested namespace blocks chain
    assert by_name["S"].namespace == "c::d"          # `namespace c::d` compound form
    assert by_name["Anon"].namespace == ""           # anonymous namespace -> attributed to parent (global)


def test_enum_class_scoped_vs_plain():
    rf = _parse(
        """
        enum class Color : int { RED, GREEN };
        enum Plain { A, B, C };
        """
    )
    by_name = {t.name: t for t in rf.types}
    assert by_name["Color"].kind == "Enum" and by_name["Color"].is_scoped is True
    assert by_name["Color"].enum_members == ["RED", "GREEN"]
    assert by_name["Plain"].kind == "Enum" and by_name["Plain"].is_scoped is False
    assert by_name["Plain"].enum_members == ["A", "B", "C"]


def test_struct_class_union_kinds():
    rf = _parse("struct S { int a; }; class C { int b; }; union U { int i; float f; };")
    kinds = {t.name: t.kind for t in rf.types}
    assert kinds == {"S": "Struct", "C": "Class", "U": "Union"}


def test_class_heritage_and_access_specifiers():
    # Multiple inheritance; C++ has no interfaces, so every base is just a base (EXTENDS).
    rf = _parse("class Widget : public Base, private Helper, Mixin { };")
    (c,) = rf.types
    assert c.kind == "Class"
    assert [(b.name, b.access) for b in c.bases] == [
        ("Base", "public"),
        ("Helper", "private"),
        ("Mixin", "private"),   # class default access is private
    ]


def test_struct_base_default_access_is_public():
    rf = _parse("struct D : Base { };")
    (d,) = rf.types
    assert [(b.name, b.access) for b in d.bases] == [("Base", "public")]


def test_members_fields_methods_static_virtual_const():
    rf = _parse(
        """
        class C {
        public:
            C(int n);
            virtual ~C();
            virtual double area() const;
            static int count();
            const Point& ref() const;
        private:
            int size_;
            static int total_;
            Point origin_;
        };
        """
    )
    (c,) = rf.types
    methods = {m.name: m for m in c.methods}
    assert methods["C"].kind == "CONSTRUCTOR" and methods["C"].return_type is None
    assert [(p.type, p.name) for p in methods["C"].params] == [("int", "n")]
    assert methods["~C"].kind == "DESTRUCTOR" and methods["~C"].is_virtual is True
    assert methods["area"].is_virtual and methods["area"].is_const and methods["area"].return_type == "double"
    assert methods["count"].is_static is True
    assert methods["ref"].is_const and methods["ref"].return_type == "Point"  # return type from the `type` field
    fields = {f.name: f for f in c.fields}
    assert set(fields) == {"size_", "total_", "origin_"}
    assert fields["total_"].is_static is True and fields["size_"].is_static is False
    assert fields["origin_"].type == "Point"


def test_template_class_and_function_capture_parameters():
    rf = _parse(
        """
        template <typename T> class Box { T value_; public: T get() const; };
        template <class K, class V> class Map { };
        template <typename T> T identity(T x) { return x; }
        """
    )
    by_name = {t.name: t for t in rf.types}
    assert by_name["Box"].type_parameters == ["T"]
    assert by_name["Map"].type_parameters == ["K", "V"]
    fn = next(f for f in rf.functions if f.name == "identity")
    assert fn.is_definition and [(p.type, p.name) for p in fn.params] == [("T", "x")]


def test_free_function_prototype_and_definition():
    rf = _parse(
        """
        int add(int a, int b);
        int add(int a, int b) { return a + b; }
        """
    )
    names = [(f.name, f.is_definition) for f in rf.functions]
    assert ("add", False) in names and ("add", True) in names
    d = next(f for f in rf.functions if f.is_definition)
    assert d.signature == "int add(int a, int b)"
    assert [(p.type, p.name) for p in d.params] == [("int", "a"), ("int", "b")]


def test_default_argument_parameters_captured_and_typed():
    """A parameter WITH a default value (`optional_parameter_declaration`) must still be captured, and
    its type must EXCLUDE the default (`CaseSensitive`, not `CaseSensitive = ...`). Otherwise a
    declaration with defaults and its out-of-line definition (no default) get different signatures/ids,
    never merge, and the decl-only node goes stale on resync (nlohmann/Catch2 real-repo finding)."""
    rf = _parse(
        """
        M Equals(std::string const& str, CaseSensitive cs = CaseSensitive::Yes);
        M Equals(std::string const& str, CaseSensitive cs) { return M{}; }
        """
    )
    decl = next(f for f in rf.functions if not f.is_definition)
    defn = next(f for f in rf.functions if f.is_definition)
    # both params are kept (the defaulted one is NOT dropped) and the default value is excised from the
    # type (`CaseSensitive`, not `CaseSensitive = CaseSensitive::Yes`).
    assert len(decl.params) == 2
    assert decl.params[1].type == "CaseSensitive"
    # decl and def produce the SAME param types -> the same id (built from param types) -> they merge.
    assert [p.type for p in decl.params] == [p.type for p in defn.params]


def test_default_argument_type_excision_edge_cases():
    """The default value is cut at the `=` TOKEN, so a type that itself contains `=` (`decltype(a == b)`)
    or a comment/brace in the default (`= /* note */ 1`, `= {}`) does NOT corrupt the param type -- the
    declaration and its out-of-line definition still get identical param types (and ids)."""
    for decl_src, def_src in [
        ("void f(decltype(1 == 1) x = true);", "void f(decltype(1 == 1) x) {}"),
        ("void f(decltype(true ? 1 : 2) x = 1);", "void f(decltype(true ? 1 : 2) x) {}"),  # ternary ':'
        ("void f(int x = /* fallback */ 1);", "void f(int x) {}"),
        ("void f(std::vector<int> v = {});", "void f(std::vector<int> v) {}"),
        ("void f(const char* p = nullptr);", "void f(const char* p) {}"),
    ]:
        dp = [p.type for fn in _parse(decl_src).functions for p in fn.params]
        cp = [p.type for fn in _parse(def_src).functions for p in fn.params]
        assert dp == cp, f"{decl_src!r} -> {dp} != {cp}"


def test_out_of_line_member_definitions_owner_detection():
    rf = _parse(
        """
        double Foo::bar(int n) { return n; }
        C::C() { }
        C::~C() { }
        int A::B::deep() { return 0; }
        """
    )
    defs = {(m.owner_name, m.name): m for m in rf.member_defs}
    assert defs[("Foo", "bar")].kind == "ORDINARY" and defs[("Foo", "bar")].is_definition
    assert defs[("Foo", "bar")].owner_scope == ["Foo"]
    assert defs[("C", "C")].kind == "CONSTRUCTOR"        # final segment == owner -> ctor
    assert defs[("C", "~C")].kind == "DESTRUCTOR"        # ~C -> dtor
    assert defs[("B", "deep")].owner_scope == ["A", "B"]  # nested: owner is the last scope segment


def test_call_extraction_bare_receiver_scope_new():
    rf = _parse(
        """
        void run(Store store, Widget* w) {
            foo();
            store.put();
            w->save();
            Helper::stat();
            Widget x = new Widget();
        }
        """
    )
    (fn,) = rf.functions
    calls = {(c.name, c.receiver, c.scope, c.is_new) for c in fn.calls}
    assert ("foo", None, None, False) in calls        # bare free call
    assert ("put", "store", None, False) in calls      # obj.method()
    assert ("save", "w", None, False) in calls         # ptr->method()
    assert ("stat", None, "Helper", False) in calls    # Class::static()
    assert ("Widget", None, None, True) in calls       # new Class()


def test_guarded_header_content_is_not_lost():
    # Almost every real header wraps its body in `#ifndef FOO_HPP ... #endif`; tree-sitter nests
    # that under a preproc_ifdef. The parser must descend into it, or every header yields nothing.
    rf = _parse(
        """
        #ifndef FOO_HPP
        #define FOO_HPP
        namespace foo {
        class Bar { public: int baz(); };
        }
        #endif
        """
    )
    (bar,) = rf.types
    assert bar.name == "Bar" and bar.namespace == "foo"
    assert [m.name for m in bar.methods] == ["baz"]


# -- doc comments (shared `doc` property via core.docstrings.leading_comment) ---------------


def test_function_doc_single_and_stacked():
    rf = _parse(
        """
        // add sums two ints.
        // The result may overflow.
        int add(int a, int b) { return a + b; }
        void undocumented() {}
        """
    )
    by_name = {f.name: f for f in rf.functions}
    assert by_name["add"].doc == "add sums two ints.\nThe result may overflow."
    assert by_name["undocumented"].doc == ""


def test_method_doc_in_class():
    rf = _parse(
        """
        class Store {
        public:
            // put stores a value.
            void put(int v);
        };
        """
    )
    (c,) = rf.types
    put = next(m for m in c.methods if m.name == "put")
    assert put.doc == "put stores a value."


def test_type_doc_line_comment():
    rf = _parse(
        """
        // Store persists widgets.
        class Store { int n_; };
        """
    )
    (s,) = rf.types
    assert s.doc == "Store persists widgets."


def test_type_doc_block_comment():
    rf = _parse(
        """
        /*
         * Widget is a drawable thing.
         */
        struct Widget {};
        """
    )
    (w,) = rf.types
    assert w.doc == "Widget is a drawable thing."


def test_template_class_doc_above_wrapper():
    # The comment precedes the `template_declaration` wrapper, not the inner class_specifier;
    # doc_of(inner, outer) must fall back to the wrapper node.
    rf = _parse(
        """
        // Box holds one value.
        template <typename T> class Box { T v_; };
        """
    )
    (b,) = rf.types
    assert b.name == "Box" and b.doc == "Box holds one value."


def test_blank_line_between_comment_and_decl_is_not_doc():
    rf = _parse(
        """
        // stray comment, not attached.

        void loose() {}
        """
    )
    assert next(f for f in rf.functions if f.name == "loose").doc == ""
