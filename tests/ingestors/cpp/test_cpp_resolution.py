"""Unit tests for the project-wide C++ resolution pass (no FalkorDB): RawFile list -> graph
nodes/edges. Exercises the namespace tier, EXTENDS to project types only (and the deliberate
absence of IMPLEMENTS), out-of-line `Owner::method` definitions merging into the owning class,
template-arity ids that don't collide, DEPENDS_ON type references, CALLS resolution
(receiver-typed obj./ptr->, `Class::static`, `new` -> constructor, bare -> free function), and
the C-family INCLUDES/macro edges."""

from __future__ import annotations

from auryn.ingestors.cpp import resolution
from auryn.ingestors.cpp.file_parser import parse_source
from auryn.ingestors.cpp.ids import project_id

_PROJ = project_id("demo")


def _resolve(files: dict[str, str]):
    raw = [parse_source(src.encode(), path) for path, src in files.items()]
    items = list(resolution.resolve(raw, _PROJ))
    nodes = [i for i in items if type(i).__name__ == "GraphNode"]
    edges = [i for i in items if type(i).__name__ == "GraphEdge"]
    return nodes, edges


def _tail(k):
    return k["id"].replace(_PROJ + "/namespace/", "").replace(_PROJ + "/", "").replace(_PROJ, "~")


def _edges(edges, rel):
    return {(_tail(e.from_key), _tail(e.to_key)): (e.properties or {}) for e in edges if e.rel_type == rel}


def _nodes(nodes, label):
    return [n for n in nodes if n.label == label]


def _by_name(nodes, label, name):
    return next(n for n in nodes if n.label == label and n.properties.get("name") == name)


def test_namespace_hierarchy_materialized():
    _, edges = _resolve({
        "a.hpp": "namespace geo { class A {}; }",
        "b.hpp": "namespace geo::detail { class B {}; }",
    })
    assert _edges(edges, "HAS_NAMESPACE") == {("~", "geo"): {}}
    subs = set(_edges(edges, "HAS_SUBNAMESPACE"))
    assert ("geo", "geo::detail") in subs


def test_global_types_and_functions_hang_off_project():
    nodes, edges = _resolve({"t.cpp": "class T {}; int freefn() { return 0; }"})
    has_type = _edges(edges, "HAS_TYPE")
    has_func = _edges(edges, "HAS_FUNCTION")
    assert ("~", "~.class.t") in has_type            # no namespace -> Project HAS_TYPE
    assert any(f == "~" for f, _ in has_func)          # free function -> Project HAS_FUNCTION


def test_extends_project_types_only_and_no_implements():
    _, edges = _resolve({
        "base.hpp": "namespace p { class Base {}; struct Mixin {}; }",
        "der.hpp": "namespace p { class Derived : public Base, private Mixin {}; }",
        "orphan.hpp": "namespace p { class Orphan : public std::runtime_error {}; }",
    })
    ext = _edges(edges, "EXTENDS")
    assert ("p.class.derived", "p.class.base") in ext
    assert ext[("p.class.derived", "p.class.base")].get("access") == "public"
    assert ("p.class.derived", "p.struct.mixin") in ext  # multiple inheritance -> one edge per base
    assert ext[("p.class.derived", "p.struct.mixin")].get("access") == "private"
    # external bases (std::runtime_error) aren't project types -> no edge.
    assert not any("orphan" in f for f, _ in ext)
    # C++ has no interfaces: there is no IMPLEMENTS edge type at all.
    assert not any(e.rel_type == "IMPLEMENTS" for e in edges)


def test_out_of_line_member_definition_merges_into_class():
    nodes, edges = _resolve({
        "s.hpp": "namespace geo { class Shape { public: Shape(); virtual double area() const; }; }",
        "s.cpp": "namespace geo { Shape::Shape() {} double Shape::area() const { return 0.0; } }",
    })
    # The in-class declaration and the out-of-line definition collapse into ONE Method node.
    areas = [n for n in _nodes(nodes, "Method") if n.properties["name"] == "area"]
    assert len(areas) == 1
    assert areas[0].properties["defined"] is True       # the out-of-line body marks it defined
    assert areas[0].properties["is_virtual"] is True     # virtuality kept from the declaration
    # HAS_METHOD from the class points at that single merged method.
    hm = _edges(edges, "HAS_METHOD")
    assert any(f == "geo.class.shape" and t.endswith("method.area()") for f, t in hm)
    ctors = [n for n in _nodes(nodes, "Method") if n.properties["kind"] == "CONSTRUCTOR"]
    assert len(ctors) == 1 and ctors[0].properties["defined"] is True


def test_template_arity_ids_do_not_collide():
    nodes, _ = _resolve({
        "b.hpp": "namespace p { class Box {}; template <typename T> class Box2 {}; }",
    })
    ids = {n.key["id"] for n in _nodes(nodes, "Class")}
    # a plain type and a template are distinct; the template's arity is folded into the id.
    assert f"{_PROJ}/namespace/p.class.box" in ids
    assert f"{_PROJ}/namespace/p.class.box2`1" in ids
    box2 = next(n for n in _nodes(nodes, "Class") if n.properties["name"] == "Box2")
    assert box2.properties["is_template"] is True and box2.properties["type_parameters"] == ["T"]


def test_depends_on_param_return_and_field_types():
    _, edges = _resolve({
        "p.hpp": "namespace p { struct Store {}; class Main { Store s_; public: Store make(Store x); }; }",
    })
    dep = _edges(edges, "DEPENDS_ON")
    assert any(f.endswith("field.s_") and t == "p.struct.store" for f, t in dep)
    assert any(f.endswith("method.make(store)") and t == "p.struct.store" for f, t in dep)


def test_calls_receiver_scope_new_and_bare_free_function():
    _, edges = _resolve({
        "h.hpp": (
            "namespace p {"
            " struct Store { void put(); };"
            " Store makeStore();"
            " class Widget {"
            "   Store store_;"
            "  public:"
            "   Widget(Store s);"
            "   void save(Store* other);"
            "   static int limit();"
            " };"
            "}"
        ),
        "c.cpp": (
            "namespace p {"
            " Store makeStore() { return Store(); }"
            " Widget::Widget(Store s) : store_(s) {}"
            " void Widget::save(Store* other) {"
            "   store_.put();"          # obj.method(): field store_ (Store) -> Store::put
            "   other->put();"          # ptr->method(): param other (Store*) -> Store::put
            "   Widget::limit();"       # Class::static() -> Widget::limit
            "   Store* z = new Store();"  # new Class() -> Store's ctor (none declared -> no edge)
            "   makeStore();"           # bare -> free function
            " }"
            "}"
        ),
    })
    calls = _edges(edges, "CALLS")
    save_calls = {(f, t): p for (f, t), p in calls.items() if "widget.method.save" in f}
    assert any(t.endswith("store.method.put()") for _f, t in save_calls)           # obj. and ptr-> both -> Store::put
    assert any(save_calls[k].get("receiver_type") == "Store" for k in save_calls)
    assert any(t.endswith("widget.method.limit()") for _f, t in save_calls)         # Class::static()
    assert any(t.endswith("func.makestore()") for _f, t in save_calls)              # bare -> free function


def test_includes_and_macros():
    _, edges = _resolve({
        "geometry.hpp": "#define MAX 10\nnamespace g { class A {}; }",
        "main.cpp": "#include \"geometry.hpp\"\n#include <vector>\nint main() { return 0; }",
    })
    inc = _edges(edges, "INCLUDES")
    assert any(f.endswith("file/main.cpp") and t.endswith("file/geometry.hpp") for f, t in inc)
    # <vector> is a system header outside the project -> no File node, no INCLUDES edge.
    assert not any("vector" in t for _f, t in inc)
    defines = _edges(edges, "DEFINES")
    assert any(t.endswith("macro.MAX") for _f, t in defines)


def test_doc_property_reaches_nodes():
    nodes, _ = _resolve({
        "store.hpp": (
            "// Store persists widgets.\n"
            "class Store {\n"
            "public:\n"
            "    // put stores a value.\n"
            "    void put(int v);\n"
            "};\n"
            "// make builds a Store.\n"
            "Store make() { return Store(); }\n"
        ),
    })
    assert _by_name(nodes, "Class", "Store").properties["doc"] == "Store persists widgets."
    assert _by_name(nodes, "Method", "put").properties["doc"] == "put stores a value."
    assert _by_name(nodes, "Function", "make").properties["doc"] == "make builds a Store."
