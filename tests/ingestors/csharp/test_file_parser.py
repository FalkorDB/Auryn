"""Pure-Python unit tests for parse_file() -- no FalkorDB needed, just tree-sitter
against hand-written .cs snippets written to tmp_path.

Every shape asserted here was confirmed empirically against tree-sitter-c-sharp
0.23.5 before file_parser.py was written (see its module docstring) -- these tests
pin that behavior down so a future tree-sitter-c-sharp upgrade can't silently change
it out from under the ingestor.
"""

from __future__ import annotations

from pathlib import Path

from auryn.ingestors.csharp.file_parser import parse_file


def _parse(tmp_path: Path, source: str, filename: str = "Sample.cs"):
    path = tmp_path / filename
    path.write_text(source)
    return parse_file(str(path))


def _type(raw_file, name: str):
    return next(t for t in raw_file.types if t.name == name)


def test_block_scoped_namespace_and_class_basics(tmp_path):
    raw_file = _parse(
        tmp_path,
        """
        namespace Acme.Widgets
        {
            public abstract sealed class Foo { }
        }
        """,
    )
    # (abstract+sealed together is invalid C#, but the parser doesn't care -- this
    # just exercises that both modifiers are captured independently)
    foo = _type(raw_file, "Foo")
    assert foo.namespace == "Acme.Widgets"
    assert foo.kind == "Class"
    assert foo.accessibility == "public"
    assert "abstract" in foo.modifiers and "sealed" in foo.modifiers


def test_file_scoped_namespace_applies_to_every_following_type(tmp_path):
    raw_file = _parse(
        tmp_path,
        """
        namespace Acme.Widgets;

        public class First { }
        public class Second { }
        """,
    )
    assert {t.namespace for t in raw_file.types} == {"Acme.Widgets"}
    assert {t.name for t in raw_file.types} == {"First", "Second"}


def test_using_directives_are_collected(tmp_path):
    raw_file = _parse(
        tmp_path,
        """
        using System;
        using System.Collections.Generic;

        namespace Acme { public class Foo { } }
        """,
    )
    assert raw_file.using_namespaces == ["System", "System.Collections.Generic"]


def test_property_shapes(tmp_path):
    raw_file = _parse(
        tmp_path,
        """
        namespace Acme
        {
            public class Widget
            {
                public int Auto { get; set; }
                public int ReadOnlyAuto { get; }
                public int WithBody
                {
                    get { return 1; }
                    set { }
                }
                public int Computed => 42;
            }
        }
        """,
    )
    widget = _type(raw_file, "Widget")
    by_name = {p.name: p for p in widget.properties}

    assert by_name["Auto"].is_auto_implemented is True
    assert by_name["Auto"].has_getter is True
    assert by_name["Auto"].has_setter is True

    assert by_name["ReadOnlyAuto"].is_auto_implemented is True
    assert by_name["ReadOnlyAuto"].has_setter is False

    assert by_name["WithBody"].is_auto_implemented is False
    assert by_name["WithBody"].has_getter is True
    assert by_name["WithBody"].has_setter is True

    assert by_name["Computed"].is_auto_implemented is False
    assert by_name["Computed"].has_getter is True
    assert by_name["Computed"].has_setter is False


def test_enum_base_list_is_underlying_type_not_extends(tmp_path):
    raw_file = _parse(
        tmp_path,
        """
        namespace Acme
        {
            public enum Status : byte { Active = 1, Inactive = 2 }
        }
        """,
    )
    status = _type(raw_file, "Status")
    assert status.underlying_type == "byte"
    assert status.base_list == []
    assert [m.name for m in status.enum_members] == ["Active", "Inactive"]
    assert [m.value for m in status.enum_members] == ["1", "2"]


def test_class_base_list_captures_simple_names_including_generics_and_qualified(tmp_path):
    raw_file = _parse(
        tmp_path,
        """
        namespace Acme
        {
            public class Impl : Base<string>, IRepo<string>, System.IDisposable { }
        }
        """,
    )
    impl = _type(raw_file, "Impl")
    assert impl.base_list == ["Base", "IRepo", "IDisposable"]


def test_attributes_with_and_without_arguments(tmp_path):
    raw_file = _parse(
        tmp_path,
        """
        namespace Acme
        {
            [ApiController]
            [Route("api/[controller]")]
            public class WidgetsController { }
        }
        """,
    )
    controller = _type(raw_file, "WidgetsController")
    by_name = {a.name: a for a in controller.attributes}
    assert by_name["ApiController"].arguments == []
    assert by_name["Route"].arguments == ["api/[controller]"]


def test_parameter_modifiers_and_defaults_and_params_array(tmp_path):
    raw_file = _parse(
        tmp_path,
        """
        namespace Acme
        {
            public class Widget
            {
                public void M(ref int a, out int b, in int c, params int[] d, int e = 5) { }
            }
        }
        """,
    )
    method = _type(raw_file, "Widget").methods[0]
    by_name = {p.name: p for p in method.parameters}
    assert by_name["a"].direction == "REF"
    assert by_name["b"].direction == "OUT"
    assert by_name["c"].direction == "IN"
    assert by_name["d"].direction == "PARAMS"
    assert by_name["d"].type == "int[]"
    assert by_name["e"].direction == "VALUE"
    assert by_name["e"].has_default is True
    assert [p.position for p in method.parameters] == [0, 1, 2, 3, 4]


def test_nested_types_and_constructor_destructor(tmp_path):
    raw_file = _parse(
        tmp_path,
        """
        namespace Acme
        {
            public class Outer
            {
                public Outer() { }
                ~Outer() { }

                public class Inner { }
            }
        }
        """,
    )
    outer = _type(raw_file, "Outer")
    assert {m.kind for m in outer.methods} == {"CONSTRUCTOR", "DESTRUCTOR"}
    assert [t.name for t in outer.nested_types] == ["Inner"]
    assert outer.nested_types[0].namespace == "Acme"


def test_record_is_value_type_and_positional_parameters(tmp_path):
    raw_file = _parse(
        tmp_path,
        """
        namespace Acme
        {
            public record Widget(int Id, string Name);
            public readonly record struct Point(int X, int Y);
        }
        """,
    )
    widget = _type(raw_file, "Widget")
    assert widget.is_value_type is False
    assert [p.name for p in widget.positional_parameters] == ["Id", "Name"]

    point = _type(raw_file, "Point")
    assert point.is_value_type is True


def test_delegate_captures_return_type_and_parameters(tmp_path):
    raw_file = _parse(
        tmp_path,
        """
        namespace Acme
        {
            public delegate void Handler(object sender, EventArgs e);
        }
        """,
    )
    handler = _type(raw_file, "Handler")
    assert handler.kind == "Delegate"
    assert handler.return_type == "void"
    assert [p.name for p in handler.positional_parameters] == ["sender", "e"]


def test_interface_and_class_type_parameters_both_captured_despite_grammar_field_inconsistency(tmp_path):
    # interface_declaration field-names its type_parameter_list ("type_parameters");
    # class_declaration doesn't -- confirmed empirically, see _type_parameters().
    raw_file = _parse(
        tmp_path,
        """
        namespace Acme
        {
            public interface IRepo<T> { }
            public class Repo<T> : IRepo<T> { }
        }
        """,
    )
    assert _type(raw_file, "IRepo").type_parameters == ["T"]
    assert _type(raw_file, "Repo").type_parameters == ["T"]


def test_field_with_multiple_declarators_and_event(tmp_path):
    raw_file = _parse(
        tmp_path,
        """
        namespace Acme
        {
            public class Widget
            {
                public int X, Y;
                private readonly string _name;
                public const int MaxCount = 100;
                public event EventHandler Changed;
            }
        }
        """,
    )
    widget = _type(raw_file, "Widget")
    by_name = {f.name: f for f in widget.fields}
    assert set(by_name) == {"X", "Y", "_name", "MaxCount"}
    assert by_name["_name"].is_readonly is True
    assert by_name["MaxCount"].is_const is True
    assert by_name["MaxCount"].is_static is True  # const implies static

    assert widget.events[0].name == "Changed"
    assert widget.events[0].type == "EventHandler"


def test_operator_and_conversion_operator(tmp_path):
    raw_file = _parse(
        tmp_path,
        """
        namespace Acme
        {
            public class Money
            {
                public static Money operator +(Money a, Money b) => a;
                public static explicit operator int(Money m) => 0;
            }
        }
        """,
    )
    money = _type(raw_file, "Money")
    assert {m.kind for m in money.methods} == {"OPERATOR"}
    names = {m.name for m in money.methods}
    assert "operator+" in names
    assert any(n.startswith("operator explicit") for n in names)


def test_call_extraction_distinguishes_receiver_kinds(tmp_path):
    raw_file = _parse(
        tmp_path,
        """
        namespace Acme
        {
            public class Widget
            {
                public void M()
                {
                    Bare();
                    this.ThisQualified();
                    _service.OnField();
                }
            }
        }
        """,
    )
    calls = {c.name: c.receiver for c in _type(raw_file, "Widget").methods[0].calls}
    assert calls["Bare"] is None
    assert calls["ThisQualified"] == "this"
    assert calls["OnField"] == "_service"


def test_never_raises_on_invalid_source(tmp_path):
    # Best-effort, consistent with the rest of the project (PL/SQL's parse_unit()
    # never raises either) -- garbage input just yields whatever partially parses.
    raw_file = _parse(tmp_path, "this is not valid C# at all {{{")
    assert isinstance(raw_file.types, list)


# -- doc comments (shared `doc` property via core.docstrings.leading_comment) ---------------
# `///` XML-doc lines and `//` line comments are both named `comment` sibling nodes
# preceding a declaration in tree-sitter-c-sharp, so leading_comment captures them and
# clean_comment strips the markers (the XML tags themselves stay).


def test_type_and_method_doc_from_xml_and_line_comments(tmp_path):
    raw_file = _parse(
        tmp_path,
        """
        namespace Acme
        {
            /// <summary>Widget does things.</summary>
            /// <remarks>More.</remarks>
            [Serializable]
            public class Widget
            {
                // Gets the value.
                [HttpGet]
                public int Get() { return 0; }
            }
        }
        """,
    )
    widget = _type(raw_file, "Widget")
    # `///` XML docs, above the attribute, both lines, markers stripped (tags kept).
    assert widget.doc == "<summary>Widget does things.</summary>\n<remarks>More.</remarks>"
    # A plain `//` block, also captured (leading_comment is marker-agnostic).
    assert widget.methods[0].doc == "Gets the value."


def test_blank_line_between_comment_and_decl_is_not_doc(tmp_path):
    raw_file = _parse(
        tmp_path,
        """
        namespace Acme
        {
            public class Widget
            {
                // detached -- a blank line below breaks the attachment

                public void Loose() { }
            }
        }
        """,
    )
    assert _type(raw_file, "Widget").methods[0].doc == ""


def test_httpclient_calls_capture_url_and_verb(tmp_path):
    # ADR 0006: HttpClient calls surface (url, verb); a non-route arg (`cache.GetAsync("key")`)
    # is NOT mistaken for HTTP (name matches, but the arg isn't route-shaped).
    raw = _parse(
        tmp_path,
        """
        namespace Bff {
          public class Gateway {
            public async Task Fetch(object w) {
              await _client.GetAsync("/api/widgets");
              await _client.PostAsJsonAsync("/api/widgets", w);
              var x = cache.GetAsync("key");
            }
          }
        }
        """,
    )
    calls = _type(raw, "Gateway").methods[0].calls
    http = {(c.http_method, c.http_url) for c in calls if c.http_url}
    assert http == {("GET", "/api/widgets"), ("POST", "/api/widgets")}
    assert all(c.http_url is None for c in calls if c.receiver == "cache")  # cache.GetAsync("key") skipped


def test_httpclient_interpolated_url_becomes_route_param(tmp_path):
    raw = _parse(
        tmp_path,
        """
        namespace Bff {
          public class Gateway {
            public async Task ById(int id) { await _client.GetAsync($"/api/widgets/{id}"); }
          }
        }
        """,
    )
    calls = _type(raw, "Gateway").methods[0].calls
    assert {(c.http_method, c.http_url) for c in calls if c.http_url} == {("GET", "/api/widgets/{param}")}
