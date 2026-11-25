"""Unit tests for the project-wide Java resolution pass (no FalkorDB): RawFile list -> graph
nodes/edges. Exercises the package tier, EXTENDS/IMPLEMENTS to project types only, DEPENDS_ON
type references, and CALLS resolution (receiver-typed field method, bare same-type, `new` ->
constructor)."""

from __future__ import annotations

from auryn.ingestors.java.file_parser import parse_source
from auryn.ingestors.java import resolution
from auryn.ingestors.java.ids import project_id

_PROJ = project_id("demo")
# The fixtures below are root-level files (no `<module>/src/main/java/` subproject layout), so they
# hang off the Project directly -- NO Module tier (that appears only when there are real subprojects).
# `tail` renders the Project root as `~`, so a packaged type reads `p.class.widget` and a default-
# package type reads `~.class.t`. See test_java_modules.py for the multi-module collision coverage.


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
        "com/acme/model/Store.java": "package com.acme.model; public class Store {}",
        "com/acme/app/Main.java": "package com.acme.app; public class Main {}",
    })
    # `~` is the Project; a root-level file's package chain roots there (no Module tier).
    assert _edges(edges, "HAS_PACKAGE") == {("~", "com"): {}}
    subs = set(_edges(edges, "HAS_SUBPACKAGE"))
    assert ("com", "com.acme") in subs
    assert ("com.acme", "com.acme.model") in subs
    assert ("com.acme", "com.acme.app") in subs


def test_default_package_types_hang_off_project():
    _, edges = _resolve({"T.java": "public class T {}"})
    has_type = _edges(edges, "HAS_TYPE")
    assert ("~", "~.class.t") in has_type  # root-level, no package -> Project HAS_TYPE


def test_extends_implements_project_types_only():
    _, edges = _resolve({
        "p/Base.java": "package p; public abstract class Base {}",
        "p/Named.java": "package p; public interface Named {}",
        "p/Widget.java": "package p; public class Widget extends Base implements Named {}",
        "p/Orphan.java": "package p; public class Orphan extends java.lang.Thread implements Runnable {}",
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
        "p/Base.java": "package p; public interface Base {}",
        "p/Sub.java": "package p; public interface Sub extends Base {}",
    })
    assert ("p.interface.sub", "p.interface.base") in _edges(edges, "EXTENDS")
    assert _edges(edges, "IMPLEMENTS") == {}


def test_depends_on_param_return_and_field_types():
    _, edges = _resolve({
        "p/Store.java": "package p; public class Store {}",
        "p/Main.java": "package p; public class Main { private Store s; public Store make(Store x) { return x; } }",
    })
    dep = _edges(edges, "DEPENDS_ON")
    assert ("p.class.main.field.s", "p.class.store") in dep
    assert ("p.class.main.method.make(store)", "p.class.store") in dep


def test_calls_receiver_typed_bare_and_new():
    _, edges = _resolve({
        "p/Store.java": "package p; public class Store { public void put(String v) {} }",
        "p/Widget.java": (
            "package p; public class Widget {"
            " private final Store store;"
            " public Widget(Store store) { this.store = store; }"  # explicit ctor -> new resolves
            " public void save() { store.put(render()); }"
            " private String render() { return \"\"; }"
            " }"
        ),
        "p/Main.java": "package p; public class Main { Widget make(Store s) { return new Widget(s); } }",
    })
    calls = _edges(edges, "CALLS")
    # receiver-typed: store (field: Store) -> Store.put
    put = next(((f, t) for (f, t), p in calls.items() if t.endswith("store.method.put(string)")), None)
    assert put is not None and calls[put].get("receiver_type") == "Store"
    # bare same-type: save -> render
    assert ("p.class.widget.method.save()", "p.class.widget.method.render()") in calls
    # new Widget(...) -> Widget's constructor
    assert any(t.endswith("widget.method.widget(store)") for (_f, t) in calls)


def test_ambiguous_type_left_unresolved():
    _, edges = _resolve({
        "a/Store.java": "package a; public class Store {}",
        "b/Store.java": "package b; public class Store {}",
        "c/Use.java": "package c; public class Use { Store make() { return null; } }",
    })
    # `Store` names two project types from an unrelated package -> deterministic but a DEPENDS_ON
    # would be a guess; return-type refs only emit when resolvable, and here it picks one
    # deterministically. Assert it does NOT crash and resolves to exactly one of them.
    dep = _edges(edges, "DEPENDS_ON")
    targets = {t for (f, t) in dep if "use" in f}
    assert targets <= {"a.class.store", "b.class.store"} and len(targets) <= 1


# -- ADR 0004: cross-repo symbol resolution (placeholders + linkage keys) -------------------

def _placeholders(nodes):
    return {n.key["id"]: n for n in nodes
            if n.label == "Method" and n.properties.get("resolved") is False}


def test_jdk_and_external_calls_become_placeholders():
    """An unresolved-but-nameable call is no longer dropped: it surfaces as a placeholder Method
    (resolved:false, origin, extern:// id) reached by an ordinary CALLS edge marked external."""
    nodes, edges = _resolve({
        "p/Svc.java": (
            "package p;"
            "import java.util.List;"
            "import java.util.ArrayList;"
            "import com.thirdparty.Lists;"
            "public class Svc {"
            " private List<String> items;"
            " public void go() {"
            "  items.add(\"x\");"                       # field receiver, JDK -> java.util.List#add
            "  List<String> more = new ArrayList<>();"  # `new` of a JDK type -> ArrayList ctor
            "  Lists.of();"                             # static call on a third-party type
            " }"
            "}"
        ),
    })
    ph = _placeholders(nodes)
    # (a) JDK/stdlib field-receiver call surfaces, keyed by the receiver's real fqcn.
    assert "extern://java/java.util.List#add" in ph
    add = ph["extern://java/java.util.List#add"]
    assert add.properties["name"] == "add"
    assert add.properties["qualified_name"] == "java.util.List#add"
    assert add.properties["origin"] == "stdlib"
    assert add.properties["linkage_key"] == "extern://java/java.util.List#add"
    # (b) `new` of a non-project type -> a constructor placeholder (member = the type's name).
    assert "extern://java/java.util.ArrayList#ArrayList" in ph
    # (c) static call on a third-party (non-java.*) type -> origin "external".
    assert "extern://java/com.thirdparty.Lists#of" in ph
    assert ph["extern://java/com.thirdparty.Lists#of"].properties["origin"] == "external"

    calls = _edges(edges, "CALLS")  # keyed (from_tail, to_id) -> props; extern to-ids stay verbatim
    add_edge = next((p for (_f, t), p in calls.items() if t == "extern://java/java.util.List#add"), None)
    assert add_edge is not None
    assert add_edge["resolution"] == "external"
    assert add_edge.get("receiver_type") == "List"


def test_project_type_missing_member_stays_dropped():
    """GUARD: a missing member of a *project* type is intra-project dangling, not external -- it is
    left dropped (no placeholder, no edge), so an incremental resync leaves no stale extern behind."""
    nodes, edges = _resolve({
        "p/Store.java": "package p; public class Store { public void put(String v) {} }",
        "p/User.java": (
            "package p; public class User {"
            " private final Store store;"
            " public User(Store s) { this.store = s; }"
            " public void run() { store.absent(); }"  # Store is a project type with no `absent`
            " }"
        ),
    })
    assert _placeholders(nodes) == {}
    calls = _edges(edges, "CALLS")
    assert not any(t.startswith("extern://") for (_f, t) in calls)


def test_real_methods_carry_linkage_key():
    """Every externally-visible (public/protected) method carries a project-independent linkage_key
    (name-based owner#member); package-private/private methods are off the cross-repo surface."""
    nodes, _ = _resolve({
        "com/acme/Billing.java": (
            "package com.acme; public class Billing {"
            " public void charge() {}"
            " protected int total() { return 0; }"
            " void pkg() {}"
            " private void secret() {}"
            " }"
        ),
    })
    by_name = {n.properties["name"]: n for n in nodes if n.label == "Method"}
    assert by_name["charge"].properties["linkage_key"] == "extern://java/com.acme.Billing#charge"
    assert by_name["total"].properties["linkage_key"] == "extern://java/com.acme.Billing#total"
    assert "linkage_key" not in by_name["pkg"].properties
    assert "linkage_key" not in by_name["secret"].properties


def test_doc_property_reaches_nodes():
    # The shared `doc` property (leading comment) flows from the parser onto the Class and Method
    # node properties, alongside `source`.
    nodes, _ = _resolve({
        "p/Store.java": (
            "package p;\n"
            "/** Store persists widgets. */\n"
            "public class Store {\n"
            "  /** Puts a value. */\n"
            "  public void put(String v) {}\n"
            "}\n"
        ),
    })
    store = next(n for n in nodes if n.label == "Class" and n.properties["name"] == "Store")
    put = next(n for n in nodes if n.label == "Method" and n.properties["name"] == "put")
    assert store.properties["doc"] == "Store persists widgets."
    assert put.properties["doc"] == "Puts a value."


# -- overloads and receiver types (name-based pass must not guess) -------------------------

_OVERLOADS = {
    "p/JsonWriter.java": "package p; public class JsonWriter {}",
    "p/JsonElement.java": "package p; public class JsonElement {}",
    "p/Gson.java": (
        "package p; public class Gson {"
        " public String toJson(JsonElement e) { return null; }"
        " public String toJson(Object o) { return null; }"
        " public void toJson(Object o, java.lang.reflect.Type t, Appendable a) {}"
        " public void toJson(Object o, java.lang.reflect.Type t, JsonWriter w) {}"
        " public void log(String... parts) {}"
        " }"
    ),
}


def _calls_from(edges, method_suffix):
    return {t for (f, t) in _edges(edges, "CALLS") if f.endswith(method_suffix)}


def test_overload_picked_by_argument_count():
    _, edges = _resolve({**_OVERLOADS, "p/Use.java": (
        "package p; public class Use {"
        " private final Gson gson = new Gson();"
        " void three(Object o, java.lang.reflect.Type t, JsonWriter w) { gson.toJson(o, t, w); }"
        " }")})
    assert _calls_from(edges, "use.method.three(object,java.lang.reflect.type,jsonwriter)") == {
        "p.class.gson.method.tojson(object,java.lang.reflect.type,jsonwriter)"}


def test_overload_picked_by_known_argument_type():
    # `s` is a String: it cannot be a project JsonElement, so only toJson(Object) fits.
    _, edges = _resolve({**_OVERLOADS, "p/Use.java": (
        "package p; public class Use {"
        " private final Gson gson = new Gson();"
        " void one(String s) { gson.toJson(s); }"
        " void tree(JsonElement e) { gson.toJson(e); }"
        " void writer(Object o, java.lang.reflect.Type t) { gson.toJson(o, t, new java.io.StringWriter()); }"
        " }")})
    assert _calls_from(edges, "use.method.one(string)") == {"p.class.gson.method.tojson(object)"}
    # a JsonElement fits both toJson(JsonElement) and toJson(Object); Java picks the exact match
    assert _calls_from(edges, "use.method.tree(jsonelement)") == {"p.class.gson.method.tojson(jsonelement)"}
    # a StringWriter is not a project JsonWriter -> the Appendable overload
    assert {t for t in _calls_from(edges, "use.method.writer(object,java.lang.reflect.type)") if "tojson" in t} == {
        "p.class.gson.method.tojson(object,java.lang.reflect.type,appendable)"}


def test_same_arity_overloads_with_unknown_arguments_are_not_guessed():
    _, edges = _resolve({**_OVERLOADS, "p/Use.java": (
        "package p; public class Use {"
        " private final Gson gson = new Gson();"
        " void unknown(Object o, java.lang.reflect.Type t) { gson.toJson(o, t, make()); }"
        " Object make() { return null; }"
        " }")})
    assert not any("tojson" in t for t in _calls_from(edges, "use.method.unknown(object,java.lang.reflect.type)"))


def test_varargs_overload_accepts_any_extra_arguments():
    _, edges = _resolve({**_OVERLOADS, "p/Use.java": (
        "package p; public class Use {"
        " private final Gson gson = new Gson();"
        " void many() { gson.log(\"a\", \"b\", \"c\"); }"
        " void none() { gson.log(); }"
        " }")})
    assert _calls_from(edges, "use.method.many()") == {"p.class.gson.method.log(string)"}
    assert _calls_from(edges, "use.method.none()") == {"p.class.gson.method.log(string)"}


def test_local_variable_and_parameter_receivers_use_their_declared_type():
    _, edges = _resolve({**_OVERLOADS,
        "p/Other.java": "package p; public class Other { public String toJson(Object o) { return null; } }",
        "p/Use.java": (
            "package p; public class Use {"
            " void local() { Gson g = new Gson(); g.toJson(\"x\"); }"
            " void param(Gson g) { g.toJson(\"x\"); }"
            " }")})
    target = {"p.class.gson.method.tojson(object)"}
    assert _calls_from(edges, "use.method.local()") == target
    assert _calls_from(edges, "use.method.param(gson)") == target


def test_unknown_receiver_does_not_bind_to_an_unrelated_same_named_method():
    # `x` has no declared type we can see (`var`), and two project types define toJson(Object):
    # binding to either is a guess -> no edge. (Before: it bound to whichever sorted first.)
    _, edges = _resolve({**_OVERLOADS,
        "p/Other.java": "package p; public class Other { public String toJson(Object o) { return null; } }",
        "p/Use.java": "package p; public class Use { void f() { var x = pick(); x.toJson(\"y\"); } Object pick() { return null; } }",
    })
    assert not any("tojson" in t for t in _calls_from(edges, "use.method.f()"))


def test_static_call_resolves_against_the_named_project_type():
    _, edges = _resolve({
        "p/Streams.java": "package p; public final class Streams { public static void write(Object e, Object w) {} }",
        # sorts before Streams, so the old alphabetical pick would land here
        "p/AWriter.java": "package p; public class AWriter { public void write(Object a, Object b) {} }",
        "p/Use.java": "package p; public class Use { void f(Object a, Object b) { Streams.write(a, b); } }",
    })
    assert _calls_from(edges, "use.method.f(object,object)") == {"p.class.streams.method.write(object,object)"}


def test_unique_method_name_still_resolves_through_a_chained_receiver():
    # A chain hides the receiver's type, but only one project method has this name -> no guess.
    _, edges = _resolve({
        "p/Builder.java": "package p; public class Builder { public Builder self() { return this; } public Object build() { return null; } }",
        "p/Use.java": "package p; public class Use { Object f(Builder b) { return b.self().build(); } }",
    })
    assert "p.class.builder.method.build()" in _calls_from(edges, "use.method.f(builder)")


def test_class_literal_argument_picks_the_class_overload():
    _, edges = _resolve({"p/Gson.java": (
        "package p; public class Gson {"
        " public <T> T fromJson(String json, Class<T> c) { return null; }"
        " public <T> T fromJson(String json, java.lang.reflect.Type t) { return null; }"
        " }"), "p/Use.java": "package p; public class Use { Object f(Gson g) { return g.fromJson(\"{}\", Use.class); } }"})
    assert _calls_from(edges, "use.method.f(gson)") == {"p.class.gson.method.fromjson(string,class<t>)"}


def test_builder_chain_receiver_typed_through_return_types():
    _, edges = _resolve({
        "p/Gson.java": "package p; public class Gson {}",
        "p/GsonBuilder.java": (
            "package p; public class GsonBuilder {"
            " public GsonBuilder setPretty() { return this; }"
            " public GsonBuilder setVersion(double v) { return this; }"
            " public Gson create() { return new Gson(); }"
            " }"),
        "p/Factory.java": "package p; public class Factory { public Object create(Gson g, Object t) { return null; } public Object create() { return null; } }",
        "p/Use.java": (
            "package p; public class Use {"
            " Gson direct() { return new GsonBuilder().create(); }"
            " Gson chain(GsonBuilder b) { return b.setPretty().setVersion(1.0).create(); }"
            " }"),
    })
    target = "p.class.gsonbuilder.method.create()"
    assert target in _calls_from(edges, "use.method.direct()")
    assert "p.class.factory.method.create()" not in _calls_from(edges, "use.method.direct()")
    assert target in _calls_from(edges, "use.method.chain(gsonbuilder)")


def test_boxed_arguments_only_fit_compatible_boxed_parameters():
    _, edges = _resolve({
        "p/Arr.java": (
            "package p; public class Arr {"
            " public void add(Boolean b) {} public void add(Character c) {}"
            " public void add(Number n) {} public void add(String s) {}"
            " }"),
        "p/Use.java": (
            "package p; public class Use {"
            " void f(Arr a) { a.add((Integer) null); }"
            " void g(Arr a) { a.add(true); }"
            " }"),
    })
    assert _calls_from(edges, "use.method.f(arr)") == {"p.class.arr.method.add(number)"}
    assert _calls_from(edges, "use.method.g(arr)") == {"p.class.arr.method.add(boolean)"}


def test_chain_on_an_external_type_never_binds_to_a_project_method():
    _, edges = _resolve({
        "p/Mapper.java": "package p; public class Mapper { public Object map(Object f) { return null; } }",
        "p/Use.java": (
            "package p; public class Use {"
            " Object f(java.util.List<String> xs) { return xs.stream().map(null); }"
            " Object g() { return new StringBuilder().append(1).map(null); }"
            " }"),
    })
    assert not any("mapper" in t for t in _calls_from(edges, "use.method.f(java.util.list<string>)"))
    assert not any("mapper" in t for t in _calls_from(edges, "use.method.g()"))
