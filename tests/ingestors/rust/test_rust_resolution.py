"""Unit tests for the project-wide Rust resolution pass (no FalkorDB): RawFile list -> graph
nodes/edges. Exercises the module tier (HAS_MODULE/HAS_SUBMODULE), crate-root items hanging off
their Crate (issue #5 -- the scope root is the crate, not the Project), impl-block method
attachment (inherent + `impl Trait for Type`, incl. no-self associated functions), IMPLEMENTS/
EXTENDS resolution to project types only, DEPENDS_ON type references, and CALLS resolution (self
method, receiver-typed param, `Type::new()` associated fn, and free function).

All the single-file fixtures below live at the repo root (no Cargo.toml / `src` layout), so every
item is rooted at the implicit repo-root crate. `tail()` normalizes that crate root to `~`, so the
assertions read the same as before the crate tier existed -- see test_rust_cargo.py for the
multi-crate collision/target coverage."""

from __future__ import annotations

from auryn.ingestors.rust.file_parser import parse_source
from auryn.ingestors.rust import resolution
from auryn.ingestors.rust.ids import crate_id, project_id

_PROJ = project_id("demo")
# The implicit repo-root crate every root-level `.rs` fixture maps to (cargo.KIND_LIB, name "crate").
_ROOT_CRATE = crate_id(_PROJ, "", "lib", "crate")


def _resolve(files: dict[str, str]):
    raw = [parse_source(src.encode(), path) for path, src in files.items()]
    items = list(resolution.resolve(raw, _PROJ))
    nodes = [i for i in items if type(i).__name__ == "GraphNode"]
    edges = [i for i in items if type(i).__name__ == "GraphEdge"]
    return nodes, edges


def _edges(edges, rel):
    def tail(k):
        return k["id"].replace(_ROOT_CRATE + "/module/", "").replace(_ROOT_CRATE, "~")
    return {(tail(e.from_key), tail(e.to_key)): (e.properties or {}) for e in edges if e.rel_type == rel}


def _nodes(nodes, label):
    return {n.properties.get("name"): n for n in nodes if n.label == label}


def test_module_tier_materialized():
    _, edges = _resolve({
        "lib.rs": "pub mod geometry { pub mod shapes { pub struct C; } }",
    })
    assert ("~", "geometry") in _edges(edges, "HAS_MODULE")
    assert ("geometry", "geometry.shapes") in _edges(edges, "HAS_SUBMODULE")


def test_crate_root_items_hang_off_crate():
    # Crate-root items hang off their Crate (issue #5), which `tail` renders as `~` for the root crate.
    nodes, edges = _resolve({"lib.rs": "pub struct T; pub fn f() {} pub const K: u32 = 1;"})
    assert ("~", "~.struct.t") in _edges(edges, "HAS_TYPE")
    assert ("~", "~.fn.f()") in _edges(edges, "HAS_FUNCTION")
    assert ("~", "~.const.k") in _edges(edges, "HAS_CONST")


def test_implements_and_extends_project_types_only():
    _, edges = _resolve({
        "m.rs": (
            "mod m {"
            " pub trait Named {}"
            " pub trait Shape: Named { fn area(&self) -> f64; }"
            " pub struct Circle;"
            " impl Named for Circle {}"
            " impl Shape for Circle { fn area(&self) -> f64 { 0.0 } }"
            " pub struct Orphan;"
            " impl std::fmt::Debug for Orphan {}"  # external trait -> no IMPLEMENTS edge
            " }"
        ),
    })
    impl = _edges(edges, "IMPLEMENTS")
    ext = _edges(edges, "EXTENDS")
    assert ("m.struct.circle", "m.trait.named") in impl
    assert ("m.struct.circle", "m.trait.shape") in impl
    assert ("m.trait.shape", "m.trait.named") in ext  # supertrait bound -> EXTENDS
    # `impl Debug for Orphan` names no project trait -> no IMPLEMENTS edge from Orphan.
    assert not any("orphan" in f for f, _ in impl)


def test_associated_fn_and_methods_attached_to_type():
    nodes, edges = _resolve({
        "lib.rs": (
            "pub struct Store { pub items: u32 }"
            " impl Store {"
            "   pub fn new() -> Store { Store { items: 0 } }"  # associated fn (no self)
            "   pub fn size(&self) -> u32 { self.items }"       # method
            " }"
        ),
    })
    has_method = _edges(edges, "HAS_METHOD")
    assert ("~.struct.store", "~.struct.store.method.new()") in has_method
    assert ("~.struct.store", "~.struct.store.method.size()") in has_method
    methods = _nodes(nodes, "Method")
    assert methods["new"].properties["is_associated"] is True
    assert methods["size"].properties["is_associated"] is False


def test_depends_on_param_return_and_field_types():
    _, edges = _resolve({
        "lib.rs": (
            "pub struct Store;"
            " pub struct Holder { store: Store }"
            " pub fn make(x: &Store) -> Store { x }"
        ),
    })
    dep = _edges(edges, "DEPENDS_ON")
    # field type reference
    assert ("~.struct.holder.field.store", "~.struct.store") in dep
    # param + return type references from the free function
    assert ("~.fn.make(&store)", "~.struct.store") in dep


def test_calls_self_receiver_typed_path_and_free():
    _, edges = _resolve({
        "lib.rs": (
            "pub struct Store;"
            " impl Store {"
            "   pub fn new() -> Store { Store }"
            "   pub fn put(&mut self, v: u32) {}"
            "   pub fn save(&mut self, other: &mut Store) {"
            "     other.put(seed());"   # receiver-typed: other: &mut Store -> Store::put
            "     self.put(1);"          # self.m() -> Store::put (same target, deduped)
            "   }"
            " }"
            " pub fn seed() -> u32 { 0 }"
            " pub fn build() -> Store {"
            "   let s = Store::new();"   # Type::new() -> Store::new (associated)
            "   seed();"                  # free call -> seed
            "   s"
            " }"
        ),
    })
    calls = _edges(edges, "CALLS")
    # receiver-typed / self -> Store::put
    assert ("~.struct.store.method.save(&mut store)", "~.struct.store.method.put(u32)") in calls
    # free call inside save -> seed
    assert ("~.struct.store.method.save(&mut store)", "~.fn.seed()") in calls
    # path associated fn -> Store::new
    assert ("~.fn.build()", "~.struct.store.method.new()") in calls
    # free call -> seed
    assert ("~.fn.build()", "~.fn.seed()") in calls
    assert all(p.get("resolution") == "name" for p in calls.values())


def test_stdlib_call_surfaces_as_placeholder():
    # ADR 0004: an unresolved-but-nameable call is a first-class placeholder Function reached by an
    # ordinary CALLS edge, not a dropped edge. A `std::`/`core::`/`alloc::` path -> origin "stdlib".
    nodes, edges = _resolve({
        "lib.rs": "pub fn f() { let mut a = 1; let mut b = 2; std::mem::swap(&mut a, &mut b); }",
    })
    swap = _nodes(nodes, "Function")["swap"]
    assert swap.key["id"] == "extern://rust/std::mem::swap"
    assert swap.properties["resolved"] is False
    assert swap.properties["origin"] == "stdlib"
    assert swap.properties["qualified_name"] == "std::mem::swap"
    assert swap.properties["linkage_key"] == "extern://rust/std::mem::swap"
    assert _edges(edges, "CALLS")[("~.fn.f()", "extern://rust/std::mem::swap")]["resolution"] == "external"


def test_other_crate_path_call_is_external_placeholder():
    nodes, edges = _resolve({
        "lib.rs": "pub fn g() { othercrate::helper::compute(); }",
    })
    compute = _nodes(nodes, "Function")["compute"]
    assert compute.key["id"] == "extern://rust/othercrate::helper::compute"
    assert compute.properties["origin"] == "external"
    assert ("~.fn.g()", "extern://rust/othercrate::helper::compute") in _edges(edges, "CALLS")


def test_method_call_on_external_type_is_method_placeholder():
    nodes, edges = _resolve({
        "lib.rs": (
            "pub struct Wrapper { s: String }"
            " impl Wrapper { pub fn describe(&self) -> String { self.s.clone() } }"
        ),
    })
    clone = _nodes(nodes, "Method")["clone"]
    assert clone.key["id"] == "extern://rust/String::clone"
    assert clone.properties["resolved"] is False and clone.properties["origin"] == "external"
    edge = _edges(edges, "CALLS")[("~.struct.wrapper.method.describe()", "extern://rust/String::clone")]
    assert edge["resolution"] == "external" and edge["receiver_type"] == "String"


def test_intra_project_dangling_calls_are_dropped_not_placeheld():
    # A miss inside a project module/type/`crate::` path is intra-project dangling, not external:
    # left dropped (no placeholder) so a resync doesn't leave a stale node.
    nodes, edges = _resolve({
        "lib.rs": (
            "pub mod m { pub fn real() {} }"
            " pub struct T;"
            " impl T { pub fn a(&self) { self.missing(); } }"  # missing self-method -> dropped
            " pub fn caller() { m::gone(); crate::also_gone(); }"  # project module / crate-relative
        ),
    })
    ext = [n for n in nodes if n.properties.get("resolved") is False]
    assert ext == []  # nothing nameable-external here
    calls = _edges(edges, "CALLS")
    assert not any(t.startswith("extern://") for _, t in calls)


def test_real_pub_callables_carry_cross_repo_linkage_key():
    nodes, _ = _resolve({
        "lib.rs": (
            "pub fn compute() {}"                                 # crate-root free fn
            " mod util { pub fn helper() {} fn hidden() {} }"     # nested module (hidden = not pub)
            " pub struct S; impl S { pub fn make() -> S { S } }"  # method
        ),
    })
    fns = _nodes(nodes, "Function")
    assert fns["compute"].properties["linkage_key"] == "extern://rust/demo::compute"
    assert fns["helper"].properties["linkage_key"] == "extern://rust/demo::util::helper"
    assert "linkage_key" not in fns["hidden"].properties  # non-pub -> not externally visible
    assert _nodes(nodes, "Method")["make"].properties["linkage_key"] == "extern://rust/S::make"


def test_doc_property_reaches_nodes():
    # The shared `doc` (leading comment) reaches the properties of every callable and named-type
    # node, alongside `source` (docs/graph-model.md).
    nodes, _ = _resolve({
        "lib.rs": (
            "/// Store persists widgets.\n"
            "pub struct Store { items: u32 }\n"
            "/// Status of a thing.\n"
            "pub enum Status { A }\n"
            "/// A drawable shape.\n"
            "pub trait Shape { fn area(&self) -> f64; }\n"
            "/// New builds a Store.\n"
            "pub fn new_store() -> u32 { 0 }\n"
            "impl Store {\n"
            "    /// Put stores a value.\n"
            "    pub fn put(&mut self, v: u32) {}\n"
            "}\n"
        ),
    })
    assert _nodes(nodes, "Struct")["Store"].properties["doc"] == "Store persists widgets."
    assert _nodes(nodes, "Enum")["Status"].properties["doc"] == "Status of a thing."
    assert _nodes(nodes, "Trait")["Shape"].properties["doc"] == "A drawable shape."
    assert _nodes(nodes, "Function")["new_store"].properties["doc"] == "New builds a Store."
    assert _nodes(nodes, "Method")["put"].properties["doc"] == "Put stores a value."


def test_ambiguous_type_resolves_deterministically_or_not_at_all():
    _, edges = _resolve({
        "a.rs": "mod a { pub struct Store; }",
        "b.rs": "mod b { pub struct Store; }",
        "c.rs": "mod c { pub fn make() -> Store { Store } }",
    })
    dep = _edges(edges, "DEPENDS_ON")
    targets = {t for (f, t) in dep if "make" in f}
    # `Store` names two project types from unrelated modules -> a single deterministic pick, never both.
    assert targets <= {"a.struct.store", "b.struct.store"} and len(targets) <= 1


def test_unresolved_calls_flagged_on_the_function_node():
    # a resolvable intra-crate call -> 0; a generic-receiver method (type un-nameable) + a macro-internal
    # call -> counted, so the node flags that its callees may be incomplete (#792)
    nodes, _ = _resolve({
        "lib.rs": (
            "pub fn helper() -> u32 { 1 }\n"
            "pub fn clean() -> u32 { helper() }\n"
            'pub fn messy<T>(x: T) { x.mystery_method(); println!("{}", format_val()); }\n'
        )
    })
    fns = _nodes(nodes, "Function")
    assert fns["clean"].properties["unresolved_calls"] == 0
    # x.mystery_method() (generic receiver, un-nameable) + format_val() inside println! -> >= 2
    assert fns["messy"].properties["unresolved_calls"] >= 2
