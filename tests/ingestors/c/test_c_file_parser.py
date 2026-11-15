"""Unit tests for the C tree-sitter parser (no FalkorDB). Confirms the empirically-derived
grammar handling: preprocessor include/define, typedefs, records + fields, enums, globals,
function prototypes vs definitions, params, calls, and -- critically -- that guarded header
content (inside `#ifndef ... #endif`) is not lost."""

from __future__ import annotations

from auryn.ingestors.c.file_parser import parse_source


def _parse(src: str):
    return parse_source(src.encode(), "t.c")


def test_includes_macros_typedefs_enums_globals():
    rf = _parse(
        """
        #include <stdio.h>
        #include "widget.h"
        #define MAX 100
        #define SQ(x) ((x)*(x))
        typedef unsigned long ulong_t;
        enum Color { RED, GREEN, BLUE };
        int counter = 0;
        static int flag;
        """
    )
    assert {(i.path, i.is_system) for i in rf.includes} == {("stdio.h", True), ("widget.h", False)}
    assert {(m.name, m.is_function_like) for m in rf.macros} == {("MAX", False), ("SQ", True)}
    assert [(t.name, t.underlying) for t in rf.typedefs] == [("ulong_t", "unsigned long")]
    assert [(e.name, e.members) for e in rf.enums] == [("Color", ["RED", "GREEN", "BLUE"])]
    assert {(g.name, g.is_static) for g in rf.globals} == {("counter", False), ("flag", True)}


def test_struct_fields_and_type_refs():
    rf = _parse("struct Widget { int id; char *name; struct Point origin; };")
    (rec,) = rf.records
    assert rec.kind == "struct" and rec.name == "Widget" and rec.is_definition
    assert [(f.name, f.type_ref) for f in rec.fields] == [("id", None), ("name", None), ("origin", "Point")]


def test_prototype_vs_definition_and_calls():
    rf = _parse(
        """
        int add(int a, int b);
        static int helper(int n) { return n; }
        int add(int a, int b) { return helper(a) + b; }
        """
    )
    protos = [f for f in rf.functions if not f.is_definition]
    defs = [f for f in rf.functions if f.is_definition]
    assert [f.name for f in protos] == ["add"]
    assert {f.name: f.is_static for f in defs} == {"helper": True, "add": False}
    add_def = next(f for f in defs if f.name == "add")
    assert [(p.type, p.name) for p in add_def.params] == [("int", "a"), ("int", "b")]
    assert add_def.calls == ["helper"]


def test_guarded_header_content_is_not_lost():
    # Everything real lives inside `#ifndef FOO_H ... #endif` (tree-sitter nests it under a
    # preproc_ifdef). The parser must descend into it, or every header yields nothing.
    rf = _parse(
        """
        #ifndef FOO_H
        #define FOO_H
        struct Foo { int x; };
        int foo(void);
        #endif
        """
    )
    assert [r.name for r in rf.records] == ["Foo"]
    assert [f.name for f in rf.functions] == ["foo"]
    assert next(f for f in rf.functions if f.name == "foo").params == []  # (void) is no params


def test_void_and_variadic_params():
    rf = _parse("int f(void); int g(const char *fmt, ...);")
    f = next(x for x in rf.functions if x.name == "f")
    g = next(x for x in rf.functions if x.name == "g")
    assert f.params == []
    # Parameter types are recorded precisely, as written -- `const`/`*`/`[]` live in the
    # declarator (outside the `type` field) and are stitched back in. Variadic '...' is not a
    # parameter_declaration, so it's dropped -> one named param. (REFERENCES resolution still
    # strips down to the bare tag via _bare() at emit time.)
    assert [(p.type, p.name) for p in g.params] == [("const char *", "fmt")]


def test_precise_signatures_and_field_types():
    rf = _parse(
        """
        struct Widget {
            const char *name;
            int dims[4];
            struct Widget *parent;
        };
        const struct Widget *widget_find(const char *name, struct Widget **out, int flags[]);
        static int helper(void) { return 0; }
        """
    )
    # Function header captured verbatim: precise return type, name, and full precise param list,
    # body excluded. Whitespace is normalized to single spaces.
    find = next(f for f in rf.functions if f.name == "widget_find")
    assert find.signature == (
        "const struct Widget *widget_find(const char *name, struct Widget **out, int flags[])"
    )
    assert [(p.type, p.name) for p in find.params] == [
        ("const char *", "name"),
        ("struct Widget **", "out"),
        ("int []", "flags"),
    ]
    # Storage class is part of the written header.
    helper = next(f for f in rf.functions if f.name == "helper")
    assert helper.signature == "static int helper(void)"
    # Field types keep `const`, `*`, and array extents.
    widget = next(r for r in rf.records if r.name == "Widget")
    assert [(f.type, f.name) for f in widget.fields] == [
        ("const char *", "name"),
        ("int [4]", "dims"),
        ("struct Widget *", "parent"),
    ]


# -- doc comments (shared `doc` property via core.docstrings.leading_comment) ---------------


def test_function_doc_single_and_stacked():
    rf = _parse(
        """
        // widget_new allocates a Widget.
        // The caller owns the result.
        struct Widget *widget_new(void) { return 0; }
        int undocumented(void) { return 0; }
        """
    )
    by_name = {f.name: f for f in rf.functions}
    assert by_name["widget_new"].doc == "widget_new allocates a Widget.\nThe caller owns the result."
    assert by_name["undocumented"].doc == ""


def test_prototype_doc():
    rf = _parse(
        """
        // add sums two ints.
        int add(int a, int b);
        """
    )
    (proto,) = rf.functions
    assert proto.name == "add" and not proto.is_definition
    assert proto.doc == "add sums two ints."


def test_record_and_enum_doc_single_line():
    rf = _parse(
        """
        // Widget is a drawable thing.
        struct Widget { int id; };
        // Color enumerates the palette.
        enum Color { RED, GREEN, BLUE };
        """
    )
    (rec,) = rf.records
    (en,) = rf.enums
    assert rec.doc == "Widget is a drawable thing."
    assert en.doc == "Color enumerates the palette."


def test_typedef_doc_block_comment():
    rf = _parse(
        """
        /*
         * ulong_t is a wide unsigned integer.
         */
        typedef unsigned long ulong_t;
        """
    )
    (td,) = rf.typedefs
    assert td.doc == "ulong_t is a wide unsigned integer."


def test_inline_record_doc_from_wrapping_typedef():
    # tree-sitter wrinkle: the comment sits above the `type_definition`, not the inner
    # struct_specifier, so the record picks it up via the outer node.
    rf = _parse(
        """
        // Point is a 2D coordinate.
        typedef struct Point { int x; int y; } Point;
        """
    )
    (rec,) = rf.records
    assert rec.name == "Point" and rec.doc == "Point is a 2D coordinate."


def test_blank_line_between_comment_and_decl_is_not_doc():
    rf = _parse(
        """
        // stray comment, not attached.

        void loose(void) {}
        """
    )
    assert next(f for f in rf.functions if f.name == "loose").doc == ""
