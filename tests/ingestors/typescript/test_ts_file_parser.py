"""Unit tests for the TypeScript/JavaScript tree-sitter parser (no FalkorDB). Confirms the
empirically-derived grammar handling: the import/export forms, class heritage + members
(incl. modifiers and parameter properties), interfaces/enums/type-aliases, free functions
including `const f = () => ...`, call/`new` extraction with receivers, and the .ts/.js/.d.ts
dialect distinction that lets one language own the whole family."""

from __future__ import annotations

from auryn.ingestors.typescript.file_parser import parse_source


def _parse(src: str, path: str = "t.ts"):
    return parse_source(src.encode(), path)


def test_dialect_and_declaration_by_suffix():
    assert _parse("export const x = 1;", "a.ts").dialect == "ts"
    assert _parse("export const x = 1;", "a.tsx").dialect == "ts"
    assert _parse("export const x = 1;", "a.js").dialect == "js"
    assert _parse("export const x = 1;", "a.mjs").dialect == "js"
    assert _parse("export interface C { x: number }", "c.d.ts").is_declaration is True
    assert _parse("export const x = 1;", "a.ts").is_declaration is False


def test_import_forms():
    rf = _parse(
        """
        import { Store, Thing as T } from "./store";
        import Default from "../default";
        import * as ns from "lib";
        import "./side-effect";
        export { A, B as C } from "./re";
        export * from "./all";
        """
    )
    by_module = {i.module: i for i in rf.imports}
    assert by_module["./store"].kind == "named"
    assert by_module["./store"].names == ["Store", "T"]  # alias wins
    assert by_module["../default"].kind == "default"
    assert by_module["lib"].kind == "namespace" and by_module["lib"].names == ["ns"]
    assert by_module["./side-effect"].kind == "side_effect"
    assert by_module["./re"].kind == "reexport" and by_module["./re"].names == ["A", "C"]
    assert by_module["./all"].kind == "reexport" and by_module["./all"].names == ["*"]


def test_class_heritage_members_and_modifiers():
    rf = _parse(
        """
        export abstract class Widget extends Base implements Named, Other {
          name: string;
          private readonly store: Store;
          static count = 0;
          optional?: number;
          constructor(private repo: Repo, plain: X) { super(); }
          save(): void { helper(); }
          get label(): string { return this.name; }
        }
        """
    )
    (c,) = rf.classes
    assert c.name == "Widget" and c.is_abstract and c.is_exported
    assert c.extends == ["Base"] and c.implements == ["Named", "Other"]

    props = {p.name: p for p in c.properties}
    # `repo` is a constructor parameter-property; the rest are field definitions.
    assert set(props) == {"name", "store", "count", "optional", "repo"}
    assert props["store"].accessibility == "private" and props["store"].is_readonly
    assert props["count"].is_static
    assert props["optional"].is_optional
    assert props["repo"].accessibility == "private" and props["repo"].type == "Repo"

    methods = {m.name: m for m in c.methods}
    assert methods["constructor"].kind == "CONSTRUCTOR"
    assert methods["save"].kind == "METHOD" and methods["save"].return_type == "void"
    assert methods["label"].kind == "GETTER"


def test_interface_enum_type_alias():
    rf = _parse(
        """
        export interface Named extends Base, Other {
          name: string;
          readonly id: number;
          greet(x: number, y?: string): void;
        }
        export enum Color { RED, GREEN = 2, BLUE }
        export type Id = string | number | Widget;
        """
    )
    (it,) = rf.interfaces
    assert it.extends == ["Base", "Other"]
    assert {p.name for p in it.properties} == {"name", "id"}
    (greet,) = it.methods
    assert greet.name == "greet" and [p.name for p in greet.params] == ["x", "y"]
    assert greet.params[1].optional is True

    (en,) = rf.enums
    assert en.name == "Color" and en.members == ["RED", "GREEN", "BLUE"]

    (ta,) = rf.type_aliases
    assert ta.name == "Id" and "Widget" in ta.type_refs


def test_functions_params_and_arrow():
    rf = _parse(
        """
        export function make(s: Store, n = 3, ...rest: number[]): Widget { return new Widget(s); }
        export const build = (s: Store): Widget => make(s);
        const plain = 42;
        async function fetchIt(): Promise<void> { await load(); }
        """
    )
    fns = {f.name: f for f in rf.functions}
    assert set(fns) == {"make", "build", "fetchIt"}  # `plain` (a value binding) is deferred
    assert fns["make"].kind == "FUNCTION" and fns["make"].is_exported
    params = fns["make"].params
    assert [(p.name, p.type) for p in params] == [("s", "Store"), ("n", ""), ("rest", "number[]")]
    assert params[1].optional is True  # has a default
    assert params[2].is_rest is True
    assert fns["build"].kind == "ARROW"
    assert fns["fetchIt"].is_async is True


def test_call_extraction_receiver_and_new():
    rf = _parse(
        """
        function f(store: Store) {
          foo();
          store.put(1);
          this.bar();
          new Widget();
        }
        """
    )
    (f,) = rf.functions
    calls = {(c.name, c.receiver, c.is_new) for c in f.calls}
    assert ("foo", None, False) in calls
    assert ("put", "store", False) in calls  # simple-name receiver captured
    assert ("bar", "this", False) in calls
    assert ("Widget", None, True) in calls


def test_expression_bodied_arrow_calls_are_captured():
    # regression: an arrow whose body IS the call (no statement block) must not be skipped.
    rf = _parse("export const build = (s: Store) => make(s);")
    (f,) = rf.functions
    assert [c.name for c in f.calls] == ["make"]


def test_jsx_and_plain_js_parse():
    # A React component in a .jsx file must parse (tsx grammar), and JS carries no types.
    rf = _parse("export function App(props) { return foo(); }", "App.jsx")
    (f,) = rf.functions
    assert f.name == "App" and f.params[0].type == "" and [c.name for c in f.calls] == ["foo"]


def test_http_client_calls_capture_url_and_method():
    # ADR 0006: HTTP-client calls surface (url, method) for the downstream call->endpoint stitch.
    rf = _parse(
        """
        function load() {
            fetch("/api/widgets");
            $.ajax({ url: "/Widgets/Save", type: "POST" });
            axios.get("/api/widgets/5");
            $.post("/Widgets/Delete");
        }
        """,
        "load.js",
    )
    (f,) = rf.functions
    http = {(c.http_method, c.http_url) for c in f.calls if c.http_url}
    assert http == {
        (None, "/api/widgets"),        # fetch, no options -> method None (GET at stitch time)
        ("POST", "/Widgets/Save"),     # $.ajax: url from `url:`, method from `type:`
        ("GET", "/api/widgets/5"),     # axios.get -> verb from the call name
        ("POST", "/Widgets/Delete"),   # $.post -> verb from the call name
    }


def test_configured_axios_client_calls_are_captured():
    # The dominant real React idiom: a configured client instance, not bare axios.*.
    rf = _parse(
        """
        const api = axios.create({ baseURL: "/api" });
        function load(body) {
            api.get("/widgets");
            api.post("/widgets", body);
        }
        """,
        "client.ts",
    )
    load = next(f for f in rf.functions if f.name == "load")
    http = {(c.http_method, c.http_url) for c in load.calls if c.http_url}
    assert http == {("GET", "/widgets"), ("POST", "/widgets")}


def test_non_http_get_is_not_captured():
    # A collection/map `.get(...)` must NOT be treated as HTTP -- no false positive, no arg walk.
    rf = _parse(
        """
        function f(store) {
            store.get("key");
            const m = new Map();
            m.get("x");
            localCache.post("y");
        }
        """,
        "f.js",
    )
    (f,) = rf.functions
    assert all(c.http_url is None and c.http_method is None for c in f.calls)


# -- doc comments (shared `doc` property via core.docstrings.leading_comment) ---------------


def test_function_doc_jsdoc_and_line_and_none():
    rf = _parse(
        """
        /**
         * make builds a Widget.
         * The result is fresh.
         */
        function make() {}
        // helper does a thing.
        function helper() {}
        function undocumented() {}
        """
    )
    by_name = {f.name: f for f in rf.functions}
    assert by_name["make"].doc == "make builds a Widget.\nThe result is fresh."
    assert by_name["helper"].doc == "helper does a thing."
    assert by_name["undocumented"].doc == ""


def test_export_function_jsdoc_attaches_via_outer_wrapper():
    # The JSDoc sits above the OUTER `export_statement`, not the inner function_declaration.
    rf = _parse(
        """
        /** exported builds a thing. */
        export function exported() {}
        """
    )
    (f,) = rf.functions
    assert f.doc == "exported builds a thing."


def test_arrow_const_doc_from_declaration():
    # `const f = () => ...`: the comment is above the lexical_declaration / export wrapper,
    # never the arrow value node.
    rf = _parse(
        """
        /** build wraps make. */
        export const build = (s: Store) => make(s);
        // plain arrow doc.
        const plain = () => 1;
        """
    )
    by_name = {f.name: f for f in rf.functions}
    assert by_name["build"].doc == "build wraps make."
    assert by_name["plain"].doc == "plain arrow doc."


def test_class_interface_and_method_doc():
    rf = _parse(
        """
        /** Widget is drawable. */
        export class Widget {
          /** save persists it. */
          save(): void {}
        }
        /** Named names things. */
        export interface Named {
          /** greet says hi. */
          greet(): void;
        }
        """
    )
    (c,) = rf.classes
    assert c.doc == "Widget is drawable."
    assert {m.name: m.doc for m in c.methods} == {"save": "save persists it."}
    (it,) = rf.interfaces
    assert it.doc == "Named names things."
    assert {m.name: m.doc for m in it.methods} == {"greet": "greet says hi."}


def test_enum_and_type_alias_doc():
    rf = _parse(
        """
        /** Color enumerates colors. */
        export enum Color { RED, GREEN }
        /** Id identifies things. */
        export type Id = string | number;
        """
    )
    assert rf.enums[0].doc == "Color enumerates colors."
    assert rf.type_aliases[0].doc == "Id identifies things."


def test_blank_line_between_comment_and_decl_is_not_doc():
    rf = _parse(
        """
        // This is a stray comment, not attached.

        export function loose() {}
        """
    )
    assert next(f for f in rf.functions if f.name == "loose").doc == ""


def test_template_string_url_becomes_route_param():
    # `api/widgets/${id}` -> api/widgets/{param}, so a templated caller matches a route template.
    rf = _parse(
        """
        const api = axios.create({ baseURL: "/api" });
        function load(id) { api.get(`/api/widgets/${id}`); }
        """,
        "t.ts",
    )
    load = next(f for f in rf.functions if f.name == "load")
    assert {c.http_url for c in load.calls if c.http_url} == {"/api/widgets/{param}"}
