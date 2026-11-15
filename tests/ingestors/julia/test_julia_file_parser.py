"""Unit tests for the Julia tree-sitter parser (no FalkorDB). Confirms the empirically-derived
grammar handling: module nesting, using/import (incl. selected imports), abstract/primitive/
struct type heads (with `<:` supertype and `{T}` parametrics), struct fields (typed + bare),
mutable marker, generic-function methods in both long (`function f ... end`) and short
(`f(x) = ...`) forms with return-type annotations, `where` type vars, keyword args, anonymous
dispatch params (`::T`) and varargs, call extraction (bare + `recv.name` receiver), and that
plain assignments / consts are NOT mistaken for function definitions."""

from __future__ import annotations

from auryn.ingestors.julia.file_parser import parse_source


def _parse(src: str, path: str = "T.jl"):
    return parse_source(src.encode(), path)


def test_module_nesting_and_imports():
    rf = _parse(
        """
        module Outer
        using LinearAlgebra
        import Base: show, length
        module Inner
            foo() = 1
        end
        end
        """
    )
    assert [m.name for m in rf.modules] == ["Outer"]
    outer = rf.modules[0]
    assert [s.name for s in outer.submodules] == ["Inner"]
    assert [m.name for m in outer.submodules[0].functions] == ["foo"]
    usings = {u.module: u for u in rf.usings}
    assert usings["LinearAlgebra"].is_import is False and usings["LinearAlgebra"].names == []
    assert usings["Base"].is_import is True and usings["Base"].names == ["show", "length"]


def test_abstract_types_with_and_without_supertype():
    rf = _parse(
        """
        module M
        abstract type Shape end
        abstract type Polygon <: Shape end
        end
        """
    )
    by_name = {t.name: t for t in rf.modules[0].types}
    assert by_name["Shape"].kind == "AbstractType" and by_name["Shape"].supertype is None
    assert by_name["Polygon"].kind == "AbstractType" and by_name["Polygon"].supertype == "Shape"


def test_struct_fields_mutable_parametric_and_supertype():
    rf = _parse(
        """
        struct Point
            x::Float64
            y::Float64
        end
        mutable struct Circle{T} <: Shape
            center::Point
            radius::T
            label
        end
        """
    )
    by_name = {t.name: t for t in rf.types}  # top-level (no module) -> hang off RawFile
    pt = by_name["Point"]
    assert pt.kind == "Struct" and pt.is_mutable is False
    assert [(f.name, f.type) for f in pt.fields] == [("x", "Float64"), ("y", "Float64")]
    circle = by_name["Circle"]
    assert circle.is_mutable is True
    assert circle.type_parameters == ["T"]
    assert circle.supertype == "Shape"  # `{T}` on the LHS stripped, base name only
    assert [(f.name, f.type) for f in circle.fields] == [
        ("center", "Point"), ("radius", "T"), ("label", ""),  # bare untyped field -> type ""
    ]


def test_primitive_type():
    rf = _parse("primitive type MyByte 8 end")
    (t,) = rf.types
    assert t.kind == "PrimitiveType" and t.name == "MyByte" and t.bits == 8


def test_short_and_long_methods_return_type_and_where():
    rf = _parse(
        """
        area(c::Circle) = 3.14 * c.radius
        function area(c::Circle, scale::Float64)::Float64 where T
            return scale
        end
        """
    )
    methods = rf.functions
    short = next(m for m in methods if m.is_short)
    long = next(m for m in methods if not m.is_short)
    assert short.name == "area" and [p.type for p in short.parameters] == ["Circle"]
    assert long.name == "area"
    assert [(p.name, p.type) for p in long.parameters] == [("c", "Circle"), ("scale", "Float64")]
    assert long.return_type == "Float64"
    assert long.type_parameters == ["T"]


def test_param_shapes_keyword_anonymous_and_varargs():
    rf = _parse(
        """
        function f(x::Int, ::String, rest::Any...; verbose::Bool=false)
        end
        """
    )
    (m,) = rf.functions
    got = [(p.name, p.type, p.is_keyword, p.is_vararg) for p in m.parameters]
    assert got == [
        ("x", "Int", False, False),
        ("", "String", False, False),   # anonymous dispatch parameter `::String`
        ("rest", "Any", False, True),   # typed varargs `rest::Any...`
        ("verbose", "Bool", True, False),  # keyword arg after `;`
    ]


def test_call_extraction_bare_and_receiver():
    rf = _parse(
        """
        function m()
            foo()
            obj.bar()
            Base.show(x)
            p = Point(1, 2)
        end
        """
    )
    (m,) = rf.functions
    calls = {(c.name, c.receiver) for c in m.calls}
    assert ("foo", None) in calls
    assert ("bar", "obj") in calls
    assert ("show", "Base") in calls
    assert ("Point", None) in calls


def test_doc_string_docstring_above_function():
    # Julia's canonical doc is a `"""..."""` string literal placed immediately above the def
    # (the `@doc` convention) -- a string node, NOT a comment; it must still be captured.
    rf = _parse(
        '"""\n'
        "    add(a, b)\n"
        "\n"
        "Adds two numbers.\n"
        '"""\n'
        "function add(a, b)\n"
        "    return a + b\n"
        "end\n"
    )
    (m,) = rf.functions
    assert m.name == "add"
    assert m.doc == "add(a, b)\n\nAdds two numbers."  # quote-stripped + dedented


def test_doc_hash_comment_above_struct():
    # The other case: a leading `#` comment block (handled by the shared leading_comment helper).
    rf = _parse(
        "# A point in 2D space.\n"
        "struct Point\n"
        "    x::Float64\n"
        "end\n"
    )
    (t,) = rf.types
    assert t.name == "Point" and t.doc == "A point in 2D space."


def test_doc_string_docstring_above_module():
    rf = _parse(
        '"""Top-level module docs."""\n'
        "module M\n"
        "  greet() = 1\n"
        "end\n"
    )
    (mod,) = rf.modules
    assert mod.name == "M" and mod.doc == "Top-level module docs."


def test_doc_blank_line_gap_detaches():
    # A blank line between the docstring and the def means it is not attached -> no doc.
    rf = _parse(
        '"""orphan doc"""\n'
        "\n"
        "lonely() = 1\n"
    )
    (m,) = rf.functions
    assert m.name == "lonely" and m.doc == ""


def test_assignment_and_const_are_not_functions():
    rf = _parse(
        """
        const PI = 3.14159
        x = 5
        pair = (1, 2)
        """
    )
    assert rf.functions == []  # none of these define a generic-function method
    assert rf.types == []
