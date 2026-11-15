"""Unit tests for the project-wide PHP resolution pass (no FalkorDB): RawFile list -> graph
nodes/edges. Exercises the namespace tier, EXTENDS/IMPLEMENTS/USES_TRAIT to project types only,
DEPENDS_ON type references (params/return/property), free-function indexing, and CALLS resolution
(receiver-typed property/param method, bare same-type/self, static `Class::m`, `new` ->
constructor, and free-function calls)."""

from __future__ import annotations

from auryn.ingestors.php.file_parser import parse_source
from auryn.ingestors.php import resolution
from auryn.ingestors.php.ids import project_id

_PROJ = project_id("demo")


def _resolve(files: dict[str, str]):
    raw = [parse_source(src.encode(), path) for path, src in files.items()]
    items = list(resolution.resolve(raw, _PROJ))
    nodes = [i for i in items if type(i).__name__ == "GraphNode"]
    edges = [i for i in items if type(i).__name__ == "GraphEdge"]
    return nodes, edges


def _edges(edges, rel):
    def tail(k):
        return k["id"].replace(_PROJ + "/namespace/", "").replace(_PROJ, "~")
    return {(tail(e.from_key), tail(e.to_key)): (e.properties or {}) for e in edges if e.rel_type == rel}


def test_namespace_hierarchy_materialized():
    _, edges = _resolve({
        "Store.php": "<?php namespace App\\Models; class Store {}",
        "Main.php": "<?php namespace App\\Web; class Main {}",
    })
    assert _edges(edges, "HAS_NAMESPACE") == {("~", "app"): {}}
    subs = set(_edges(edges, "HAS_SUBNAMESPACE"))
    assert ("app", "app.models") in subs
    assert ("app", "app.web") in subs


def test_global_namespace_types_hang_off_project():
    _, edges = _resolve({"T.php": "<?php class T {}"})
    assert ("~", "~.class.t") in _edges(edges, "HAS_TYPE")  # no namespace -> Project HAS_TYPE


def test_extends_implements_uses_trait_project_types_only():
    _, edges = _resolve({
        "Base.php": "<?php namespace p; abstract class Base {}",
        "Named.php": "<?php namespace p; interface Named {}",
        "T.php": "<?php namespace p; trait T {}",
        "Widget.php": "<?php namespace p; class Widget extends Base implements Named { use T; }",
        "Orphan.php": "<?php namespace p; class Orphan extends \\External\\Thing implements \\Countable {}",
    })
    assert ("p.class.widget", "p.class.base") in _edges(edges, "EXTENDS")
    assert ("p.class.widget", "p.interface.named") in _edges(edges, "IMPLEMENTS")
    assert ("p.class.widget", "p.trait.t") in _edges(edges, "USES_TRAIT")
    # external bases aren't project types -> no edge.
    assert not any("orphan" in f for f, _ in _edges(edges, "EXTENDS"))
    assert not any("orphan" in f for f, _ in _edges(edges, "IMPLEMENTS"))


def test_interface_extends_is_extends_not_implements():
    _, edges = _resolve({
        "Base.php": "<?php namespace p; interface Base {}",
        "Sub.php": "<?php namespace p; interface Sub extends Base {}",
    })
    assert ("p.interface.sub", "p.interface.base") in _edges(edges, "EXTENDS")
    assert _edges(edges, "IMPLEMENTS") == {}


def test_depends_on_param_return_and_property_types():
    _, edges = _resolve({
        "Store.php": "<?php namespace p; class Store {}",
        "Main.php": (
            "<?php namespace p; class Main {"
            " private ?Store $s;"
            " public function make(Store $x): Store { return $x; } }"
        ),
    })
    dep = _edges(edges, "DEPENDS_ON")
    assert ("p.class.main.property.s", "p.class.store") in dep
    assert ("p.class.main.method.make(store)", "p.class.store") in dep


def test_calls_receiver_typed_bare_static_and_new():
    _, edges = _resolve({
        "Store.php": "<?php namespace p; class Store { public function put(string $v): void {} }",
        "Helper.php": "<?php namespace p; class Helper { public static function stat(): void {} }",
        "Widget.php": (
            "<?php namespace p; class Widget {"
            " private Store $store;"
            " public function __construct(Store $store) { $this->store = $store; }"  # ctor -> new resolves
            " public function save(): void { $this->store->put($this->render()); Helper::stat(); }"
            " public function render(): string { return ''; } }"
        ),
        "Main.php": (
            "<?php namespace p; class Main {"
            " public function make(Store $store): Widget { return new Widget($store); } }"
        ),
    })
    calls = _edges(edges, "CALLS")
    # receiver-typed: $this->store (property: Store) -> Store.put
    put = next(((f, t) for (f, t), p in calls.items() if t.endswith("store.method.put(string)")), None)
    assert put is not None and calls[put].get("receiver_type") == "Store"
    # bare same-type: save -> render
    assert ("p.class.widget.method.save()", "p.class.widget.method.render()") in calls
    # static Helper::stat() -> Helper.stat, with receiver_type recorded
    stat = next(((f, t) for (f, t), p in calls.items() if t.endswith("helper.method.stat()")), None)
    assert stat is not None and calls[stat].get("receiver_type") == "Helper"
    # new Widget(...) -> Widget's constructor
    assert any(t.endswith("widget.method.__construct(store)") for (_f, t) in calls)


def test_free_function_calls_and_indexing():
    _, edges = _resolve({
        "helpers.php": (
            "<?php namespace app;"
            " function bootstrap(): string { return version(); }"
            " function version(): string { return '1'; }"
        ),
    })
    calls = _edges(edges, "CALLS")
    assert ("app.function.bootstrap()", "app.function.version()") in calls
    assert calls[("app.function.bootstrap()", "app.function.version()")]["resolution"] == "name"
    # a free function hangs off its namespace via HAS_FUNCTION
    assert ("app", "app.function.version()") in _edges(edges, "HAS_FUNCTION")


def _nodes_by_id(nodes):
    return {n.key["id"]: (n.label, n.properties or {}) for n in nodes}


def test_unresolved_nameable_call_becomes_placeholder():
    # ADR 0004: a nameable unresolved call is now a first-class placeholder + CALLS edge, not a
    # dropped edge. `unknownFn()` (a free function not in this project, in namespace c) surfaces as
    # `extern://php/c\unknownFn` (origin external); `$x->gone()` (untyped local, dynamic) stays
    # dropped.
    nodes, edges = _resolve({
        "Use.php": "<?php namespace c; class Use_ { public function m(): void { unknownFn(); $x->gone(); } }",
    })
    calls = _edges(edges, "CALLS")
    eid = "extern://php/c\\unknownFn"
    edge = next(((f, t) for (f, t) in calls if t == eid), None)
    assert edge is not None, calls
    assert calls[edge]["resolution"] == "external"
    label, props = _nodes_by_id(nodes)[eid]
    assert label == "Function"
    assert props["resolved"] is False
    assert props["origin"] == "external"
    assert props["qualified_name"] == "c\\unknownFn"
    assert props["linkage_key"] == eid
    # $x->gone(): untyped/unknown receiver -> dynamic, no placeholder, no edge.
    assert not any(t.endswith("gone") or "gone" in t for (_f, t) in calls if t != eid)


def _by_name(nodes, label, name):
    return next(n for n in nodes if n.label == label and n.properties.get("name") == name)


def test_doc_property_reaches_nodes():
    # The shared `doc` property (leading comment via core.docstrings.leading_comment) reaches the
    # Class, Method, and Function node properties.
    nodes, _ = _resolve({
        "Store.php": (
            "<?php\n"
            "namespace App;\n"
            "/** Store persists widgets. */\n"
            "class Store {\n"
            "    /** Put stores a value. */\n"
            "    public function put(string $v): void {}\n"
            "}\n"
            "// makeStore builds a Store.\n"
            "function makeStore(): Store { return new Store(); }\n"
        ),
    })
    assert _by_name(nodes, "Class", "Store").properties["doc"] == "Store persists widgets."
    assert _by_name(nodes, "Method", "put").properties["doc"] == "Put stores a value."
    assert _by_name(nodes, "Function", "makeStore").properties["doc"] == "makeStore builds a Store."


def test_builtin_call_surfaces_as_placeholder():
    # ADR 0004 §4: a built-in call is surfaced as a placeholder with origin="builtin" (a terminal
    # external that never resolves to an ingested repo), keyed by its bare global name.
    nodes, edges = _resolve({
        "u.php": "<?php function slugify(string $s): string { return str_replace(' ', '-', $s); }",
    })
    calls = _edges(edges, "CALLS")
    eid = "extern://php/str_replace"
    assert any(t == eid for (_f, t) in calls), calls
    assert calls[next((f, t) for (f, t) in calls if t == eid)]["resolution"] == "external"
    label, props = _nodes_by_id(nodes)[eid]
    assert label == "Function"
    assert props == {
        "name": "str_replace", "qualified_name": "str_replace",
        "resolved": False, "origin": "builtin", "linkage_key": eid,
    }


def test_external_class_static_and_method_calls_become_placeholders():
    # A `Class::method` / `$typedvar->method()` whose class is NOT a project type -> placeholder
    # Method (external), keyed by `Class::method`. A missing method of a PROJECT class stays dropped.
    nodes, edges = _resolve({
        "S.php": (
            "<?php namespace p; class Svc {"
            " private \\Vendor\\Client $client;"
            " public function run(): void { $this->client->send(); Mailer::flush(); } }"
        ),
    })
    calls = _edges(edges, "CALLS")
    by_id = _nodes_by_id(nodes)
    # $this->client (typed \Vendor\Client -> keeps its FQN) -> Vendor\Client::send
    send = "extern://php/Vendor\\Client::send"
    assert any(t == send for (_f, t) in calls), calls
    assert by_id[send][0] == "Method"
    assert by_id[send][1]["resolved"] is False and by_id[send][1]["origin"] == "external"
    # Mailer::flush() (unqualified, not a project type) -> qualified to the caller ns: p\Mailer::flush
    flush = "extern://php/p\\Mailer::flush"
    assert any(t == flush for (_f, t) in calls), calls
    assert by_id[flush][0] == "Method"


def test_real_callables_carry_linkage_key():
    # ADR 0004: every real Function/Method carries the project-independent `linkage_key` a
    # placeholder would heal onto (name-based: namespace/class + name, no param types).
    nodes, _ = _resolve({
        "B.php": (
            "<?php namespace App; function helper(): void {}"
            " class Billing { public function charge(int $cents): void {} }"
        ),
    })
    by_id = _nodes_by_id(nodes)
    fn = next(p for lbl, p in by_id.values() if lbl == "Function" and p.get("name") == "helper")
    assert fn["linkage_key"] == "extern://php/App\\helper"
    m = next(p for lbl, p in by_id.values() if lbl == "Method" and p.get("name") == "charge")
    assert m["linkage_key"] == "extern://php/App\\Billing::charge"
