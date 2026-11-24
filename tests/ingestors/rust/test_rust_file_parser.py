"""Unit tests for the Rust tree-sitter parser (no FalkorDB). Confirms the empirically-derived
grammar handling: use/const/static, the module tier (inline `mod {}` vs external `mod x;`, and
nesting), struct bodies (named + tuple + unit, field visibility, generics), enum variants
(unit/tuple/struct), trait supertraits + required/default methods, impl blocks (inherent + `impl
Trait for Type`), method receivers (`self`/`&self`/`&mut self` and no-self associated fns), and
call extraction of every shape (free / method / path, with the `foo::<T>()` turbofish unwrapped)."""

from __future__ import annotations

from auryn.ingestors.rust.file_parser import parse_source


def _parse(src: str, path: str = "T.rs"):
    return parse_source(src.encode(), path)


def test_uses_consts_and_module_kinds():
    rf = _parse(
        """
        use crate::model::Store;
        use std::collections::{HashMap, HashSet};
        pub const MAX: u32 = 10;
        static GREETING: &str = "hi";
        pub mod model { pub fn helper() -> u32 { 42 } }
        mod other;
        """
    )
    assert "crate::model::Store" in rf.uses
    assert "std::collections::HashMap" in rf.uses and "std::collections::HashSet" in rf.uses
    consts = {c.name: c for c in rf.consts}
    assert consts["MAX"].kind == "const" and consts["MAX"].type == "u32"
    assert consts["GREETING"].kind == "static"
    by_mod = {m.name: m for m in rf.modules}
    assert by_mod["model"].is_declaration is False and [f.name for f in by_mod["model"].functions] == ["helper"]
    assert by_mod["other"].is_declaration is True  # `mod other;` -- external file


def test_struct_named_tuple_unit_and_field_visibility():
    rf = _parse(
        """
        pub struct Store<T> { pub items: Vec<String>, count: usize }
        struct Point(pub i32, i32);
        struct Unit;
        """
    )
    by_name = {t.name: t for t in rf.types}
    store = by_name["Store"]
    assert store.kind == "Struct" and store.visibility == "pub" and store.type_parameters == ["T"]
    fields = {f.name: f for f in store.fields}
    assert fields["items"].visibility == "pub" and fields["items"].type == "Vec<String>"
    assert fields["count"].visibility == "private" and fields["count"].type == "usize"
    point = by_name["Point"]  # tuple struct -> positional field names "0","1"
    assert [(f.name, f.type) for f in point.fields] == [("0", "i32"), ("1", "i32")]
    assert by_name["Unit"].fields == []


def test_enum_variants():
    rf = _parse(
        """
        pub enum Status {
            Active,
            Pending(u32, String),
            Named { label: String },
        }
        """
    )
    (e,) = rf.types
    assert e.kind == "Enum"
    variants = {v.name: v for v in e.variants}
    assert variants["Active"].kind == "unit"
    assert variants["Pending"].kind == "tuple" and [f.type for f in variants["Pending"].fields] == ["u32", "String"]
    assert variants["Named"].kind == "struct" and [f.name for f in variants["Named"].fields] == ["label"]


def test_impl_methods_self_kinds_and_associated():
    rf = _parse(
        """
        struct C;
        impl C {
            pub fn new() -> C { C }
            fn area(&self, factor: f64) -> f64 { 0.0 }
            fn consume(self) {}
            fn modify(&mut self) {}
        }
        """
    )
    (impl,) = rf.impls
    assert impl.type_name == "C" and impl.trait_name is None
    methods = {m.name: m for m in impl.methods}
    assert methods["new"].is_associated is True and methods["new"].self_kind is None and methods["new"].return_type == "C"
    assert methods["new"].visibility == "pub"
    assert methods["area"].is_associated is False and methods["area"].self_kind == "ref"
    assert [(p.name, p.type) for p in methods["area"].parameters] == [("factor", "f64")]
    assert methods["consume"].self_kind == "value"
    assert methods["modify"].self_kind == "mut_ref"


def test_trait_supertraits_and_required_vs_default_methods():
    rf = _parse(
        """
        pub trait Shape: Named + Clone {
            fn area(&self) -> f64;
            fn describe(&self) -> String { String::from("x") }
        }
        """
    )
    (t,) = rf.types
    assert t.kind == "Interface" and t.supertraits == ["Named", "Clone"]
    methods = {m.name: m for m in t.trait_methods}
    assert methods["area"].has_body is False  # required (function_signature_item)
    assert methods["describe"].has_body is True  # default-bodied


def test_impl_trait_for_type():
    rf = _parse("impl Shape for Circle { fn area(&self) -> f64 { 1.0 } }")
    (impl,) = rf.impls
    assert impl.type_name == "Circle" and impl.trait_name == "Shape"
    assert [m.name for m in impl.methods] == ["area"]


def test_call_extraction_free_method_path_and_turbofish():
    rf = _parse(
        """
        fn m(store: &Store) {
            foo();
            self.tag();
            store.put(v);
            Store::new();
            other::helper();
            compute::<u32>();
        }
        """
    )
    (m,) = rf.functions
    calls = {(c.name, c.kind, c.receiver, c.qualifier) for c in m.calls}
    assert ("foo", "free", None, None) in calls
    assert ("tag", "method", "self", None) in calls
    assert ("put", "method", "store", None) in calls
    assert ("new", "path", None, "Store") in calls          # Type::assoc()
    assert ("helper", "path", None, "other") in calls        # mod::func()
    assert ("compute", "free", None, None) in calls          # foo::<T>() turbofish unwrapped


def test_nested_modules():
    rf = _parse("mod outer { mod inner { fn f() {} } fn g() {} }")
    (outer,) = rf.modules
    assert outer.name == "outer" and [f.name for f in outer.functions] == ["g"]
    (inner,) = outer.modules
    assert inner.name == "inner" and [f.name for f in inner.functions] == ["f"]


# -- doc comments (shared `doc` property via core.docstrings.leading_comment) ---------------
# tree-sitter-rust makes doc comments (`line_comment` for `///`/`//`, `block_comment` for
# `/** */`) and `#[...]` attributes (`attribute_item`) siblings of the item they decorate.


def test_function_doc_single_stacked_and_undocumented():
    rf = _parse(
        """
        /// New builds a Store.
        /// The returned Store is empty.
        pub fn new_store() -> u32 { 0 }
        fn undocumented() {}
        """
    )
    by_name = {f.name: f for f in rf.functions}
    assert by_name["new_store"].doc == "New builds a Store.\nThe returned Store is empty."
    assert by_name["undocumented"].doc == ""


def test_impl_method_doc():
    rf = _parse(
        """
        struct C;
        impl C {
            /// Put stores a value.
            pub fn put(&mut self, v: u32) {}
        }
        """
    )
    (impl,) = rf.impls
    (put,) = impl.methods
    assert put.doc == "Put stores a value."


def test_struct_doc():
    rf = _parse(
        """
        /// Store persists widgets.
        pub struct Store { items: u32 }
        """
    )
    (s,) = rf.types
    assert s.doc == "Store persists widgets."


def test_struct_doc_block_comment():
    rf = _parse(
        """
        /**
         * Widget is a drawable thing.
         */
        pub struct Widget;
        """
    )
    (w,) = rf.types
    assert w.doc == "Widget is a drawable thing."


def test_doc_above_attribute_is_captured():
    # The doc comment sits above the `#[derive(..)]` attribute (an `attribute_item` sibling that
    # stands between the comment and the struct); doc_of steps back over it.
    rf = _parse(
        """
        /// Docs above an attribute.
        #[derive(Debug, Clone)]
        pub struct Tagged { n: u32 }
        """
    )
    (t,) = rf.types
    assert t.name == "Tagged" and t.doc == "Docs above an attribute."


def test_enum_and_trait_doc():
    rf = _parse(
        """
        /// A status value.
        pub enum Status { Active }
        /// A drawable shape.
        pub trait Shape {
            /// Area of the shape.
            fn area(&self) -> f64;
        }
        """
    )
    by_name = {t.name: t for t in rf.types}
    assert by_name["Status"].doc == "A status value."
    shape = by_name["Shape"]
    assert shape.doc == "A drawable shape."
    (area,) = shape.trait_methods
    assert area.doc == "Area of the shape."  # doc on a trait required method


def test_blank_line_between_comment_and_decl_is_not_doc():
    # A `///` doc comment separated by a blank line does not attach. Exercises the newline-aware
    # gap check (tree-sitter-rust includes the trailing newline in a `///` node's span).
    rf = _parse(
        """
        /// This is a stray doc comment, not attached.

        pub fn loose() {}
        """
    )
    assert next(f for f in rf.functions if f.name == "loose").doc == ""


def test_macro_internal_calls_are_counted():
    # calls inside macros produce no CALLS edge (a macro body is a token tree) -- counted for #792
    rf = _parse('fn f() { let x = format!("{}", helper()); assert!(x.is_empty()); }')
    fn = {f.name: f for f in rf.functions}["f"]
    assert fn.unresolved_macro_calls == 2  # helper() + is_empty()


def test_macro_without_a_call_counts_zero():
    rf = _parse('fn f() { println!("hello"); let v = vec![1, 2, 3]; }')
    assert {f.name: f for f in rf.functions}["f"].unresolved_macro_calls == 0


def test_macro_internal_turbofish_call_is_counted():
    # a turbofish call inside a macro (`helper::<u32>()`) is still a call the engine can't see -- counted
    rf = _parse('fn f() { println!("{}", helper::<u32>()); }')
    assert {f.name: f for f in rf.functions}["f"].unresolved_macro_calls == 1


def test_macro_string_literal_parens_are_not_counted():
    # a `(` inside a macro's format/string literal is not a call -- it must not inflate the count
    rf = _parse('fn f() { println!("example: helper()"); }')
    assert {f.name: f for f in rf.functions}["f"].unresolved_macro_calls == 0


def test_macro_raw_string_does_not_hide_following_call():
    # a raw string `r#"..."#` may contain a bare `"`; it must not swallow a real call after it
    rf = _parse('fn f() { format!(r#"a" {} {}"#, helper(), other()); }')
    assert {f.name: f for f in rf.functions}["f"].unresolved_macro_calls == 2  # helper() + other()


def test_macro_raw_string_with_embedded_quote_counts_zero_when_callfree():
    rf = _parse('fn f() { println!(r#"value "{}" done"#); }')
    assert {f.name: f for f in rf.functions}["f"].unresolved_macro_calls == 0
