"""Unit tests for the project-wide Kotlin resolution pass (no FalkorDB): RawFile list -> graph
nodes/edges. Exercises the package tier, top-level Function/Field emission, EXTENDS/IMPLEMENTS to
project types only, DEPENDS_ON type references, and CALLS resolution (receiver-typed field method,
bare same-type, top-level function, constructor via a receiver-less type call, and ADR 0004
external placeholders + linkage keys)."""

from __future__ import annotations

from auryn.ingestors.kotlin import resolution
from auryn.ingestors.kotlin.file_parser import parse_source
from auryn.ingestors.kotlin.ids import project_id

_PROJ = project_id("demo")
# The fixtures below are root-level files (no `<module>/src/main/kotlin/` subproject layout), so they
# hang off the Project directly -- NO Module tier (that appears only when there are real subprojects).
# `tail` renders the Project root as `~`, so a packaged type reads `p.class.widget` and a default-
# package type reads `~.class.t`. See test_kotlin_modules.py for the multi-module collision coverage.


def _resolve(files: dict[str, str]):
    raw = [parse_source(src.encode(), path) for path, src in files.items()]
    items = list(resolution.resolve(raw, _PROJ))
    nodes = [i for i in items if type(i).__name__ == "GraphNode"]
    edges = [i for i in items if type(i).__name__ == "GraphEdge"]
    return nodes, edges


def _edges(edges, rel):
    def tail(k):
        return k["id"].replace(_PROJ + "/package/", "").replace(_PROJ, "~")
    return {(tail(e.from_key), tail(e.to_key)): (e.properties or {}) for e in edges if e.rel_type == rel}


def test_package_hierarchy_materialized():
    _, edges = _resolve({
        "com/acme/model/Store.kt": "package com.acme.model\nclass Store",
        "com/acme/app/Main.kt": "package com.acme.app\nclass Main",
    })
    assert _edges(edges, "HAS_PACKAGE") == {("~", "com"): {}}
    subs = set(_edges(edges, "HAS_SUBPACKAGE"))
    assert ("com", "com.acme") in subs
    assert ("com.acme", "com.acme.model") in subs
    assert ("com.acme", "com.acme.app") in subs


def test_default_package_types_hang_off_project():
    _, edges = _resolve({"T.kt": "class T"})
    assert ("~", "~.class.t") in _edges(edges, "HAS_TYPE")  # root-level, no package -> Project HAS_TYPE


def test_extends_implements_project_types_only():
    _, edges = _resolve({
        "p/Base.kt": "package p\nabstract class Base",
        "p/Named.kt": "package p\ninterface Named",
        "p/Widget.kt": "package p\nclass Widget : Base(), Named",
        "p/Orphan.kt": "package p\nclass Orphan : Thread(), Runnable",
    })
    ext = _edges(edges, "EXTENDS")
    impl = _edges(edges, "IMPLEMENTS")
    assert ("p.class.widget", "p.class.base") in ext
    assert ("p.class.widget", "p.interface.named") in impl
    # external bases (Thread/Runnable) aren't project types -> no edge.
    assert not any("orphan" in f for f, _ in ext)
    assert not any("orphan" in f for f, _ in impl)


def test_interface_extends_is_extends_not_implements():
    _, edges = _resolve({
        "p/Base.kt": "package p\ninterface Base",
        "p/Sub.kt": "package p\ninterface Sub : Base",
    })
    assert ("p.interface.sub", "p.interface.base") in _edges(edges, "EXTENDS")
    assert _edges(edges, "IMPLEMENTS") == {}


def test_object_implements_interface():
    _, edges = _resolve({
        "p/Named.kt": "package p\ninterface Named",
        "p/Registry.kt": "package p\nobject Registry : Named",
    })
    assert ("p.class.registry", "p.interface.named") in _edges(edges, "IMPLEMENTS")


def test_depends_on_param_return_and_field_types():
    _, edges = _resolve({
        "p/Store.kt": "package p\nclass Store",
        "p/Main.kt": (
            "package p\n"
            "class Main {\n"
            "  private val s: Store? = null\n"
            "  fun make(x: Store): Store { return x }\n"
            "}\n"
        ),
    })
    dep = _edges(edges, "DEPENDS_ON")
    assert ("p.class.main.field.s", "p.class.store") in dep
    assert ("p.class.main.method.make(store)", "p.class.store") in dep


def test_calls_receiver_typed_bare_and_constructor():
    _, edges = _resolve({
        "p/Store.kt": "package p\nclass Store { fun put(v: String) {} }",
        "p/Widget.kt": (
            "package p\n"
            "class Widget(private val store: Store) {\n"
            "  fun save() { store.put(render()) }\n"
            "  private fun render(): String { return \"\" }\n"
            "}\n"
        ),
        "p/Main.kt": "package p\nclass Main { fun make(s: Store): Widget { return Widget(s) } }",
    })
    calls = _edges(edges, "CALLS")
    # receiver-typed: store (field: Store) -> Store.put
    put = next(((f, t) for (f, t), p in calls.items() if t.endswith("store.method.put(string)")), None)
    assert put is not None and calls[put].get("receiver_type") == "Store"
    # bare same-type: save -> render
    assert ("p.class.widget.method.save()", "p.class.widget.method.render()") in calls
    # Widget(s) -> Widget's (primary) constructor (Kotlin has no `new`)
    assert any(t.endswith("widget.method.widget(store)") for (_f, t) in calls)


def test_top_level_function_calls_resolve():
    nodes, edges = _resolve({
        "p/Top.kt": "package p\nfun main() { greet() }\nfun greet() {}\n",
    })
    functions = {n.properties["name"] for n in nodes if n.label == "Function"}
    assert {"main", "greet"} <= functions
    assert ("p.fn.main()", "p.fn.greet()") in _edges(edges, "CALLS")
    # top-level funs hang off their Package via HAS_FUNCTION.
    assert ("p", "p.fn.greet()") in _edges(edges, "HAS_FUNCTION")


def test_top_level_property_becomes_field():
    nodes, edges = _resolve({"p/Consts.kt": "package p\nval VERSION: String = \"1.0\"\n"})
    field = next((n for n in nodes if n.label == "Field" and n.properties["name"] == "VERSION"), None)
    assert field is not None
    assert ("p", "p.field.version") in _edges(edges, "HAS_FIELD")


def test_ambiguous_type_left_unresolved():
    _, edges = _resolve({
        "a/Store.kt": "package a\nclass Store",
        "b/Store.kt": "package b\nclass Store",
        "c/Use.kt": "package c\nclass Use { fun make(): Store? { return null } }",
    })
    dep = _edges(edges, "DEPENDS_ON")
    targets = {t for (f, t) in dep if "use" in f}
    assert targets <= {"a.class.store", "b.class.store"} and len(targets) <= 1


# -- ADR 0004: cross-repo symbol resolution (placeholders + linkage keys) -------------------

def _placeholders(nodes):
    return {n.key["id"]: n for n in nodes
            if n.label == "Method" and n.properties.get("resolved") is False}


def test_stdlib_and_external_calls_become_placeholders():
    """An unresolved-but-nameable call surfaces as a placeholder Method (resolved:false, origin,
    extern:// id) reached by an ordinary CALLS edge marked external."""
    nodes, edges = _resolve({
        "p/Svc.kt": (
            "package p\n"
            "import com.thirdparty.Lists\n"
            "class Svc {\n"
            "  private var items: MutableList<String> = ArrayList()\n"
            "  fun go() {\n"
            "    items.add(\"x\")\n"   # field receiver, Kotlin stdlib -> kotlin.collections.MutableList#add
            "    Lists.of()\n"        # static call on an imported third-party type
            "    Widget()\n"          # receiver-less call of a non-project type -> external constructor
            "  }\n"
            "}\n"
        ),
    })
    ph = _placeholders(nodes)
    # (a) stdlib field-receiver call, keyed by the receiver's real fqcn.
    add_key = "extern://kotlin/kotlin.collections.MutableList#add"
    assert add_key in ph
    assert ph[add_key].properties["name"] == "add"
    assert ph[add_key].properties["qualified_name"] == "kotlin.collections.MutableList#add"
    assert ph[add_key].properties["origin"] == "stdlib"
    assert ph[add_key].properties["linkage_key"] == add_key
    # (b) static call on an imported third-party type -> origin "external".
    assert "extern://kotlin/com.thirdparty.Lists#of" in ph
    assert ph["extern://kotlin/com.thirdparty.Lists#of"].properties["origin"] == "external"
    # (c) receiver-less constructor of a non-project type -> a constructor placeholder.
    assert "extern://kotlin/Widget#Widget" in ph

    calls = _edges(edges, "CALLS")
    add_edge = next((p for (_f, t), p in calls.items() if t == add_key), None)
    assert add_edge is not None
    assert add_edge["resolution"] == "external"
    assert add_edge.get("receiver_type") == "MutableList"


def test_project_type_missing_member_stays_dropped():
    """GUARD: a missing member of a *project* type is intra-project dangling, not external -- it is
    left dropped (no placeholder, no edge)."""
    nodes, edges = _resolve({
        "p/Store.kt": "package p\nclass Store { fun put(v: String) {} }",
        "p/User.kt": (
            "package p\n"
            "class User(private val store: Store) {\n"
            "  fun run() { store.absent() }\n"  # Store is a project type with no `absent`
            "}\n"
        ),
    })
    assert _placeholders(nodes) == {}
    assert not any(t.startswith("extern://") for (_f, t) in _edges(edges, "CALLS"))


def test_real_callables_carry_linkage_key():
    """Every externally-visible (public/protected) method/function carries a project-independent
    linkage_key; private/internal ones are off the cross-repo surface."""
    nodes, _ = _resolve({
        "com/acme/Billing.kt": (
            "package com.acme\n"
            "class Billing {\n"
            "  fun charge() {}\n"                  # public (default) -> linkage
            "  protected fun total(): Int = 0\n"   # protected -> linkage
            "  internal fun helper() {}\n"         # internal (module-visible only) -> no linkage
            "  private fun secret() {}\n"          # private -> no linkage
            "}\n"
        ),
        "com/acme/util/Util.kt": "package com.acme.util\nfun compute() {}\n",  # top-level fun
    })
    methods = {n.properties["name"]: n for n in nodes if n.label == "Method"}
    assert methods["charge"].properties["linkage_key"] == "extern://kotlin/com.acme.Billing#charge"
    assert methods["total"].properties["linkage_key"] == "extern://kotlin/com.acme.Billing#total"
    assert "linkage_key" not in methods["helper"].properties
    assert "linkage_key" not in methods["secret"].properties
    compute = next(n for n in nodes if n.label == "Function" and n.properties["name"] == "compute")
    assert compute.properties["linkage_key"] == "extern://kotlin/com.acme.util.compute"


def test_doc_property_reaches_nodes():
    nodes, _ = _resolve({
        "p/Store.kt": (
            "package p\n"
            "/** Store persists widgets. */\n"
            "class Store {\n"
            "  /** Puts a value. */\n"
            "  fun put(v: String) {}\n"
            "}\n"
        ),
    })
    store = next(n for n in nodes if n.label == "Class" and n.properties["name"] == "Store")
    put = next(n for n in nodes if n.label == "Method" and n.properties["name"] == "put")
    assert store.properties["doc"] == "Store persists widgets."
    assert put.properties["doc"] == "Puts a value."
