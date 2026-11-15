"""Unit tests for the Go tree-sitter parser (no FalkorDB). Confirms the empirically-derived
grammar handling: package clause, imports (incl. aliased), struct fields + embedding (anonymous
fields, incl. `*T`), interface method specs + embedded interfaces, functions (return types,
params, multi-name params, variadic), methods with pointer/value receivers, type aliases and
named types, consts (single + block), and call/construction extraction (bare, selector with
receiver, `new(T)`, composite literals)."""

from __future__ import annotations

from auryn.ingestors.go.file_parser import parse_source


def _parse(src: str, path: str = "t.go"):
    return parse_source(src.encode(), path)


def test_package_and_imports():
    rf = _parse(
        """
        package store

        import (
            "fmt"
            m "example.com/acme/proj/model"
        )
        import "errors"
        """
    )
    assert rf.package == "store"
    by_path = {i.path: i for i in rf.imports}
    assert by_path["fmt"].alias is None
    assert by_path["errors"].alias is None
    assert by_path["example.com/acme/proj/model"].alias == "m"


def test_struct_fields_and_embedding():
    rf = _parse(
        """
        package p
        type Store struct {
            Base
            *Mixin
            items []string
            model m.Model
        }
        """
    )
    (s,) = rf.types
    assert s.kind == "Struct" and s.name == "Store"
    fields = {f.name: f for f in s.fields}
    assert set(fields) == {"items", "model"}  # embedded fields are NOT named fields
    assert fields["items"].type == "[]string"
    assert fields["model"].type == "m.Model" and fields["model"].type_refs == ["m.Model"]
    # embedded (anonymous) fields -> embeds, `*` stripped
    assert set(s.embeds) == {"Base", "Mixin"}


def test_multi_name_fields():
    rf = _parse("package p\ntype T struct { x, y int; z string }")
    (t,) = rf.types
    assert [(f.name, f.type) for f in t.fields] == [("x", "int"), ("y", "int"), ("z", "string")]


def test_interface_methods_and_embedding():
    rf = _parse(
        """
        package p
        type Repository interface {
            Reader
            fmt.Stringer
            Put(value string) error
            Size() int
        }
        """
    )
    (i,) = rf.types
    assert i.kind == "Interface"
    assert {m.name for m in i.methods} == {"Put", "Size"}
    put = next(m for m in i.methods if m.name == "Put")
    assert [(p.name, p.type) for p in put.parameters] == [("value", "string")]
    assert put.return_type == "error"
    assert set(i.embeds) == {"Reader", "fmt.Stringer"}


def test_functions_params_varargs_and_returns():
    rf = _parse(
        """
        package p
        func New(name string) *Store { return nil }
        func Helper(a, b int, xs ...string) (int, error) { return 0, nil }
        """
    )
    by_name = {f.name: f for f in rf.functions}
    new = by_name["New"]
    assert new.receiver is None
    assert [(p.name, p.type) for p in new.parameters] == [("name", "string")]
    assert new.return_type == "*Store"
    helper = by_name["Helper"]
    assert [(p.name, p.type, p.is_variadic) for p in helper.parameters] == [
        ("a", "int", False), ("b", "int", False), ("xs", "string", True),
    ]
    assert helper.return_type == "(int, error)"


def test_methods_pointer_and_value_receiver():
    rf = _parse(
        """
        package p
        func (s *Store) Put(value string) error { return nil }
        func (s Store) Size() int { return 0 }
        """
    )
    methods = {f.name: f for f in rf.functions if f.receiver is not None}
    assert methods["Put"].receiver.type_name == "Store" and methods["Put"].receiver.is_pointer is True
    assert methods["Put"].receiver.name == "s"
    assert methods["Size"].receiver.is_pointer is False


def test_generic_type_and_receiver():
    rf = _parse(
        """
        package p
        type Container[T any] struct { val T }
        func (c *Container[T]) Get() T { return c.val }
        """
    )
    (c,) = rf.types
    assert c.name == "Container" and c.type_parameters == ["T"]
    m = next(f for f in rf.functions if f.receiver is not None)
    assert m.receiver.type_name == "Container"  # `*Container[T]` -> "Container"


def test_type_alias_and_named_type():
    rf = _parse(
        """
        package p
        type Celsius = float64
        type Handler func(int) error
        """
    )
    by_name = {t.name: t for t in rf.types}
    assert by_name["Celsius"].kind == "TypeAlias" and by_name["Celsius"].is_alias is True
    assert by_name["Celsius"].underlying == "float64"
    assert by_name["Handler"].kind == "TypeAlias" and by_name["Handler"].is_alias is False


def test_consts_single_and_block():
    rf = _parse(
        """
        package p
        const MaxItems = 100
        const ( A = 1; B = 2 )
        """
    )
    consts = {c.name: c.value for c in rf.consts}
    assert set(consts) == {"MaxItems", "A", "B"}
    assert consts["MaxItems"] == "100"


def test_call_and_construction_extraction():
    rf = _parse(
        """
        package p
        func (s *Store) save() {
            foo()
            s.record()
            model.New()
            _ = &Store{}
            _ = new(Widget)
            n := len(s.items)
        }
        """
    )
    (m,) = [f for f in rf.functions if f.name == "save"]
    calls = {(c.name, c.receiver) for c in m.calls}
    assert ("foo", None) in calls          # bare call
    assert ("record", "s") in calls        # selector with receiver
    assert ("New", "model") in calls       # package-qualified call
    assert not any(c.name == "len" for c in m.calls)  # builtins skipped
    # `&Store{}` and `new(Widget)` are construction sites -> type refs, not calls
    assert "Store" in m.type_refs
    assert "Widget" in m.type_refs


# -- doc comments (shared `doc` property via core.docstrings.leading_comment) ---------------


def test_function_doc_single_and_stacked():
    rf = _parse(
        """
        package p
        // New builds a Store.
        // The returned Store is empty.
        func New() *Store { return nil }
        func Undocumented() {}
        """
    )
    by_name = {f.name: f for f in rf.functions}
    assert by_name["New"].doc == "New builds a Store.\nThe returned Store is empty."
    assert by_name["Undocumented"].doc == ""


def test_method_doc():
    rf = _parse(
        """
        package p
        type Store struct{}
        // Put stores a value.
        func (s *Store) Put(v string) {}
        """
    )
    put = next(f for f in rf.functions if f.name == "Put")
    assert put.doc == "Put stores a value."


def test_struct_doc_from_type_declaration():
    rf = _parse(
        """
        package p
        // Store persists widgets.
        type Store struct { items []string }
        """
    )
    (s,) = rf.types
    assert s.doc == "Store persists widgets."


def test_type_doc_block_comment():
    rf = _parse(
        """
        package p
        /*
         * Widget is a drawable thing.
         */
        type Widget struct{}
        """
    )
    (w,) = rf.types
    assert w.doc == "Widget is a drawable thing."


def test_interface_method_spec_doc():
    rf = _parse(
        """
        package p
        type Repo interface {
            // Get fetches by id.
            Get(id string) error
        }
        """
    )
    (i,) = rf.types
    get = next(m for m in i.methods if m.name == "Get")
    assert get.doc == "Get fetches by id."


def test_blank_line_between_comment_and_decl_is_not_doc():
    rf = _parse(
        """
        package p
        // This is a stray comment, not attached.

        func Loose() {}
        """
    )
    assert next(f for f in rf.functions if f.name == "Loose").doc == ""
