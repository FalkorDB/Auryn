"""Unit tests for the project-wide Julia resolution pass (no FalkorDB): RawFile list -> graph
nodes/edges. Exercises the module tier (HAS_MODULE/HAS_SUBMODULE; items outside any module hang
off the Project), the generic-function grouping (many Methods -> one Function via HAS_METHOD,
keyed by parameter-type text), EXTENDS to project types only, DISPATCHES_ON (Julia's dispatch
signal), DEPENDS_ON type references, and name-based CALLS (Method -> Function)."""

from __future__ import annotations

from auryn.ingestors.julia.file_parser import parse_source
from auryn.ingestors.julia import resolution
from auryn.ingestors.julia.ids import project_id

_PROJ = project_id("demo")


def _resolve(files: dict[str, str]):
    raw = [parse_source(src.encode(), path) for path, src in files.items()]
    items = list(resolution.resolve(raw, _PROJ))
    nodes = [i for i in items if type(i).__name__ == "GraphNode"]
    edges = [i for i in items if type(i).__name__ == "GraphEdge"]
    return nodes, edges


def _tail(k):
    return (
        k["id"].replace(_PROJ + "/module/", "")
        .replace(_PROJ + "/file/", "file:")
        .replace(_PROJ, "~")
    )


def _edges(edges, rel):
    return {(_tail(e.from_key), _tail(e.to_key)): (e.properties or {}) for e in edges if e.rel_type == rel}


def _nodes(nodes, label):
    return {_tail(n.key): n.properties for n in nodes if n.label == label}


def test_module_tier_and_toplevel_hangs_off_project():
    nodes, edges = _resolve({
        "shapes.jl": "module Shapes\n module Geo\n  f() = 1\n end\nend",
        "top.jl": "g() = 2\nstruct Bare end",
    })
    assert _edges(edges, "HAS_MODULE") == {("~", "shapes"): {}}
    assert _edges(edges, "HAS_SUBMODULE") == {("shapes", "shapes.geo"): {}}
    # a function/type declared outside any module hangs off the Project.
    assert ("~", "~.func.g") in _edges(edges, "HAS_FUNCTION")
    assert ("~", "~.struct.bare") in _edges(edges, "HAS_TYPE")


def test_generic_function_groups_methods():
    nodes, edges = _resolve({
        "m.jl": (
            "module M\n"
            "abstract type Shape end\n"
            "struct Circle <: Shape end\n"
            "struct Square <: Shape end\n"
            "area(c::Circle) = 1\n"
            "area(s::Square) = 2\n"
            "end"
        ),
    })
    # ONE Function `area`, TWO Methods keyed by parameter type -> both under it via HAS_METHOD.
    assert "m.func.area" in _nodes(nodes, "Function")
    has_method = _edges(edges, "HAS_METHOD")
    assert ("m.func.area", "m.func.area(circle)") in has_method
    assert ("m.func.area", "m.func.area(square)") in has_method
    assert ("m", "m.func.area") in _edges(edges, "HAS_FUNCTION")


def test_extends_project_types_only():
    _, edges = _resolve({
        "m.jl": (
            "module M\n"
            "abstract type Shape end\n"
            "abstract type Polygon <: Shape end\n"  # abstract <: abstract
            "struct Circle <: Shape end\n"          # struct   <: abstract
            "struct Loose <: AbstractFloat end\n"   # external base -> no edge
            "end"
        ),
    })
    ext = _edges(edges, "EXTENDS")
    assert ("m.abstract.polygon", "m.abstract.shape") in ext
    assert ("m.struct.circle", "m.abstract.shape") in ext
    assert not any("loose" in f for f, _ in ext)  # AbstractFloat isn't a project type


def test_dispatches_on_parameter_types():
    _, edges = _resolve({
        "m.jl": (
            "module M\n"
            "struct Circle end\n"
            "struct Square end\n"
            "combine(a::Circle, b::Square) = 0\n"
            "end"
        ),
    })
    disp = _edges(edges, "DISPATCHES_ON")
    key = ("m.func.combine(circle,square)", "m.struct.circle")
    assert key in disp
    assert ("m.func.combine(circle,square)", "m.struct.square") in disp


def test_depends_on_param_return_and_field_types():
    _, edges = _resolve({
        "m.jl": (
            "module M\n"
            "struct Store end\n"
            "mutable struct Holder\n items::Store\n end\n"
            "make(x::Store)::Store = x\n"
            "end"
        ),
    })
    dep = _edges(edges, "DEPENDS_ON")
    assert ("m.struct.holder.field.items", "m.struct.store") in dep  # field type
    assert ("m.func.make(store)", "m.struct.store") in dep           # param/return type


def test_calls_resolve_name_based_across_scopes():
    _, edges = _resolve({
        "shapes.jl": (
            "module Shapes\n"
            "struct Circle end\n"
            "area(c::Circle) = 3.14\n"
            "total(c::Circle) = area(c)\n"       # same-scope call
            "end"
        ),
        "app.jl": (
            "module App\n"
            "run(c) = total(c)\n"                # cross-scope call, resolves by name
            "end"
        ),
    })
    calls = _edges(edges, "CALLS")
    same_scope = ("shapes.func.total(circle)", "shapes.func.area")
    # `run(c)`'s param is untyped, so its method id keys on the (empty) type text -> `run()`.
    cross_scope = ("app.func.run()", "shapes.func.total")
    assert same_scope in calls and calls[same_scope] == {"resolution": "name"}
    assert cross_scope in calls and calls[cross_scope] == {"resolution": "name"}


def test_unresolved_reference_emits_placeholder():
    nodes, edges = _resolve({
        "m.jl": "module M\n use() = external_thing(1)\nend",
    })
    # ADR 0004: external_thing isn't a project function -> a first-class placeholder Function
    # (keyed by its cross-repo linkage) reached by an ordinary CALLS edge, not a dropped edge.
    eid = "extern://julia/external_thing"
    ph = _nodes(nodes, "Function")[eid]
    assert ph["resolved"] is False
    assert ph["origin"] == "external"
    assert ph["name"] == "external_thing"
    assert ph["qualified_name"] == "external_thing"
    assert ph["linkage_key"] == eid
    calls = _edges(edges, "CALLS")
    assert calls[("m.func.use()", eid)] == {"resolution": "external"}


def test_base_call_surfaces_as_stdlib_placeholder():
    nodes, edges = _resolve({
        "m.jl": 'module M\n greet() = println("hi")\nend',
    })
    # A Base/stdlib call (`println`) now surfaces as a placeholder, classified origin="stdlib".
    eid = "extern://julia/println"
    ph = _nodes(nodes, "Function")[eid]
    assert ph["resolved"] is False
    assert ph["origin"] == "stdlib"
    assert ph["linkage_key"] == eid
    assert ("m.func.greet()", eid) in _edges(edges, "CALLS")


def test_module_qualified_call_into_existing_project_module_stays_dropped():
    # `Shapes.gone(...)` names a project module that lacks the function -> intra-project dangling,
    # left dropped (not a cross-repo placeholder); a bare unknown name would placeholder instead.
    nodes, edges = _resolve({
        "m.jl": (
            "module Shapes\n area() = 1\n end\n"
            "module App\n go() = Shapes.gone(1)\n end"
        ),
    })
    assert "extern://julia/Shapes.gone" not in _nodes(nodes, "Function")
    assert not any(to == "extern://julia/Shapes.gone" for _, to in _edges(edges, "CALLS"))


def test_doc_reaches_method_node_properties():
    # The leading `"""..."""` docstring flows raw_model -> resolution onto the Method node's `doc`.
    nodes, _ = _resolve({
        "m.jl": (
            "module M\n"
            '"""Computes an area."""\n'
            "area(r) = 3.14 * r * r\n"
            "end"
        ),
    })
    method = _nodes(nodes, "Method")["m.func.area()"]
    assert method["doc"] == "Computes an area."


def test_real_function_carries_linkage_key():
    nodes, _ = _resolve({"m.jl": "module M\n area() = 1\nend"})
    fn = _nodes(nodes, "Function")["m.func.area"]
    assert fn["linkage_key"] == "extern://julia/M.area"
    assert "resolved" not in fn  # a real definition is not a placeholder
