"""Unit tests for the project-wide TS resolution pass (no FalkorDB): the RawFile list -> graph
nodes/edges, exercising the cross-file decisions -- relative import resolution (incl.
extensionless and cross-dialect .js), EXTENDS/IMPLEMENTS to project types only, RETURNS/
REFERENCES to project types, and CALLS resolution (bare function, receiver-typed method,
`new` -> constructor)."""

from __future__ import annotations

from auryn.ingestors.typescript.file_parser import parse_source
from auryn.ingestors.typescript import resolution
from auryn.ingestors.typescript.ids import project_id

_PROJ = project_id("demo")


def _resolve(files: dict[str, str]):
    raw = [parse_source(src.encode(), path) for path, src in files.items()]
    items = resolution.resolve(raw, _PROJ)
    nodes = [i for i in items if type(i).__name__ == "GraphNode"]
    edges = [i for i in items if type(i).__name__ == "GraphEdge"]
    return nodes, edges


def _edges(edges, rel):
    """(from-id-tail, to-id-tail, props) for every edge of a rel-type, ids shortened."""
    def tail(k):
        return k["id"].replace(_PROJ + "/module/", "").replace(_PROJ, "~")
    return {(tail(e.from_key), tail(e.to_key)): e.properties for e in edges if e.rel_type == rel}


def test_module_nodes_carry_dialect_and_declaration():
    nodes, edges = _resolve({
        "src/a.ts": "export const x = 1;",
        "src/b.js": "export function y() {}",
        "src/c.d.ts": "export interface I { n: number }",
    })
    mods = {n.properties["path"]: n.properties for n in nodes if n.label == "Module"}
    assert mods["src/a.ts"]["dialect"] == "ts" and mods["src/a.ts"]["is_declaration"] is False
    assert mods["src/b.js"]["dialect"] == "js"
    assert mods["src/c.d.ts"]["is_declaration"] is True
    # every module hangs off the Project.
    assert len(_edges(edges, "HAS_MODULE")) == 3


def test_import_resolution_relative_extensionless_and_crossdialect():
    _, edges = _resolve({
        "src/index.ts": 'import { y } from "./util";\nimport { z } from "./sub/z";',
        "src/util.js": "export function y() {}",
        "src/sub/z.ts": "export const z = 1;",
    })
    imports = _edges(edges, "IMPORTS")
    assert ("src/index.ts", "src/util.js") in imports  # extensionless -> .js (cross-dialect)
    assert ("src/index.ts", "src/sub/z.ts") in imports  # nested relative path
    # a bare/package specifier surfaces as an external placeholder Module (ADR 0004) -- the
    # dependency stays visible; no node_modules is invented.
    nodes2, edges2 = _resolve({"src/index.ts": 'import x from "react";'})
    assert _edges(edges2, "IMPORTS") == {
        ("src/index.ts", "extern://typescript/react"): {"kind": "default", "resolution": "external"}
    }
    react = next(n for n in nodes2 if n.key["id"] == "extern://typescript/react")
    assert react.label == "Module" and react.properties["resolved"] is False
    assert react.properties["origin"] == "external"


def test_extends_implements_resolve_to_project_types_only():
    _, edges = _resolve({
        "src/base.ts": "export class Component {}",
        "src/m.ts": "export interface Named { name: string }",
        "src/w.ts": (
            'import { Component } from "./base";\n'
            'import { Named } from "./m";\n'
            "export class Widget extends Component implements Named {}\n"
            "export class Orphan extends External {}"
        ),
    })
    assert ("src/w.ts.class.Widget", "src/base.ts.class.Component") in _edges(edges, "EXTENDS")
    assert ("src/w.ts.class.Widget", "src/m.ts.iface.Named") in _edges(edges, "IMPLEMENTS")
    # `External` is not a project type -> no edge (honest name resolution).
    assert not any("Orphan" in f for f, _ in _edges(edges, "EXTENDS"))


def test_calls_bare_receiver_and_new():
    _, edges = _resolve({
        "src/models.ts": "export class Store { put(v: string): void {} }",
        "src/index.ts": (
            'import { Store } from "./models";\n'
            "export function make(s: Store): Store { return new Store(); }\n"  # no explicit ctor -> new drops
            "export function helper(): void {}\n"
            "export const run = (s: Store): void => { helper(); s.put(\"x\"); };"
        ),
    })
    calls = _edges(edges, "CALLS")
    # bare call -> unique project function
    assert ("src/index.ts.func.run(s)", "src/index.ts.func.helper()") in calls
    # receiver-typed method: s: Store -> Store.put
    put = next((p for (f, t), p in calls.items() if t.endswith("Store.method.put(v)")), None)
    assert put is not None and put.get("receiver_type") == "Store"


def test_new_resolves_to_constructor_when_present():
    _, edges = _resolve({
        "src/w.ts": "export class Widget { constructor(n: number) {} }",
        "src/index.ts": (
            'import { Widget } from "./w";\n'
            "export function make(): Widget { return new Widget(1); }"
        ),
    })
    calls = _edges(edges, "CALLS")
    assert any(t.endswith("Widget.method.constructor(n)") for (_f, t) in calls)


def test_jsx_resolves_to_function_and_class_components():
    _, edges = _resolve({
        "src/Card.tsx": "export function Card() { return <div />; }",
        "src/Panel.tsx": "export class Panel { render() { return null; } }",
        "src/page.tsx": (
            'import { Card } from "./Card";\n'
            'import { Panel } from "./Panel";\n'
            "export function Page() { return <main><Card /><Panel /></main>; }"
        ),
    })
    calls = _edges(edges, "CALLS")
    card = calls.get(("src/page.tsx.func.Page()", "src/Card.tsx.func.Card()"))
    assert card is not None and card["resolution"] == "name"
    # a class component's usage targets its render method (React invokes it), as `new X()` targets X's ctor
    assert any(f == "src/page.tsx.func.Page()" and t.endswith("Panel.method.render()") for (f, t) in calls)
    # the intrinsic <main>/<div> resolve to nothing
    assert len(calls) == 2


def test_jsx_member_and_ambiguous_tags_are_left_unresolved():
    # Emit no edge rather than a wrong one: `<Tabs.Panel>` is a property of a value, not a class method, so
    # a unique same-named method must not capture it; a component name defined twice is ambiguous.
    _, edges = _resolve({
        "src/a.tsx": "export function Dup() { return null; }",
        "src/b.tsx": "export function Dup() { return null; }",
        "src/tabs.ts": "export class Registry { Panel(): void {} }",
        "src/page.tsx": "export function Page() { return <><Dup /><Tabs.Panel /></>; }",
    })
    assert not any(f.startswith("src/page.tsx") for (f, _t) in _edges(edges, "CALLS"))


def test_module_scope_calls_have_the_module_as_caller():
    _, edges = _resolve({
        "src/util.ts": "export function helper(): number { return 1; }",
        "src/Card.tsx": "export function Card() { return null; }",
        "src/util.test.tsx": (
            'import { helper } from "./util";\n'
            'import { Card } from "./Card";\n'
            "describe('helper', () => { it('works', () => { helper(); render(<Card />); }); });"
        ),
    })
    calls = _edges(edges, "CALLS")
    assert ("src/util.test.tsx", "src/util.ts.func.helper()") in calls
    assert ("src/util.test.tsx", "src/Card.tsx.func.Card()") in calls
    caller = next(e for e in edges if e.rel_type == "CALLS" and e.from_key["id"].endswith("src/util.test.tsx"))
    assert caller.from_label == "Module"


def test_returns_and_references_to_project_types():
    _, edges = _resolve({
        "src/models.ts": "export class Store {}",
        "src/index.ts": (
            'import { Store } from "./models";\n'
            "export function make(s: Store): Store { return s; }"
        ),
    })
    returns = _edges(edges, "RETURNS")
    refs = _edges(edges, "REFERENCES")
    assert ("src/index.ts.func.make(s)", "src/models.ts.class.Store") in returns  # return type
    assert ("src/index.ts.func.make(s)", "src/models.ts.class.Store") in refs  # param type


def test_ambiguous_type_name_is_left_unresolved():
    # two project classes named `Store` -> a bare `Store` reference resolves to neither.
    _, edges = _resolve({
        "src/a.ts": "export class Store {}",
        "src/b.ts": "export class Store {}",
        "src/c.ts": 'export function f(s: Store): void {}',
    })
    assert _edges(edges, "REFERENCES") == {}


def test_doc_property_reaches_nodes():
    nodes, _ = _resolve({
        "src/widget.ts": (
            "/** Widget is drawable. */\n"
            "export class Widget {\n"
            "  /** save persists it. */\n"
            "  save(): void {}\n"
            "}\n"
            "/** make builds a Widget. */\n"
            "export function make(): void {}\n"
        ),
    })
    def by(label, name):
        return next(n for n in nodes if n.label == label and n.properties.get("name") == name)
    assert by("Class", "Widget").properties["doc"] == "Widget is drawable."
    assert by("Method", "save").properties["doc"] == "save persists it."
    assert by("Function", "make").properties["doc"] == "make builds a Widget."


def test_http_client_call_emits_route_keyed_apiendpoint_placeholder():
    # ADR 0006: an HTTP-client call materializes a route-keyed APIEndpoint placeholder (shared
    # extern://http/ namespace, resolved:false) that the caller CALLS_API -> ; the stitch hook
    # resolves it to the handler later, cross-repo. Route is normalized (lowercased, no query).
    nodes, edges = _resolve({
        "src/api.js": """
            export function load() {
                fetch("/api/Widgets/List?page=1");
                $.post("/api/Widgets/Save");
            }
        """,
    })
    endpoints = {n.key["id"]: n.properties for n in nodes if n.label == "APIEndpoint"}
    assert set(endpoints) == {
        "extern://http/ANY:/api/widgets/list",   # fetch, no verb -> ANY; query stripped; lowercased
        "extern://http/POST:/api/widgets/save",  # $.post -> POST
    }
    ep = endpoints["extern://http/ANY:/api/widgets/list"]
    assert ep["resolved"] is False and ep["route"] == "/api/widgets/list" and ep["provenance"] == "http-derived"

    calls_api = {e.to_key["id"] for e in edges if e.rel_type == "CALLS_API"}
    assert calls_api == set(endpoints)
    assert all(e.properties.get("provenance") == "http-derived" for e in edges if e.rel_type == "CALLS_API")
