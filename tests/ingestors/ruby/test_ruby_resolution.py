"""Unit tests for the project-wide Ruby resolution pass (no FalkorDB): RawFile list -> graph
nodes/edges. Exercises the class/module tier (HAS_TYPE/HAS_MODULE, nested + reopened namespaces),
member attachment (HAS_METHOD/HAS_FUNCTION/HAS_FIELD/HAS_CONST/HAS_PARAMETER), the singleton flag,
EXTENDS (superclass) and INCLUDES (mixin) to project namespaces only, the require graph (IMPORTS),
name-based CALLS resolution (command/self/super/constant/`.new`, cross-ancestor dispatch, the
drop of dynamic instance-receiver calls), ADR-0004 external placeholders, cross-repo linkage keys,
and the shared `doc` property reaching nodes."""

from __future__ import annotations

from auryn.ingestors.ruby import resolution
from auryn.ingestors.ruby.file_parser import parse_source
from auryn.ingestors.ruby.ids import project_id

_PROJ = project_id("demo")


def _resolve(files: dict[str, str]):
    raw = [parse_source(src.encode(), path) for path, src in files.items()]
    items = list(resolution.resolve(raw, _PROJ))
    nodes = [i for i in items if type(i).__name__ == "GraphNode"]
    edges = [i for i in items if type(i).__name__ == "GraphEdge"]
    return nodes, edges


def _tail(node_id: str) -> str:
    if node_id.startswith("extern://"):
        return node_id
    return node_id.replace(_PROJ + "/", "").replace(_PROJ, "~")


def _edges(edges, rel):
    return {(_tail(e.from_key["id"]), _tail(e.to_key["id"])): (e.properties or {})
            for e in edges if e.rel_type == rel}


def _nodes(nodes, label):
    return {n.properties.get("name"): n for n in nodes if n.label == label}


def test_class_module_tier_and_nesting():
    nodes, edges = _resolve({
        "app.rb": "module Outer\n class Inner\n end\n module Deep\n end\nend\nclass Top\nend",
    })
    has_type = _edges(edges, "HAS_TYPE")
    has_module = _edges(edges, "HAS_MODULE")
    assert ("~", "class/top") in has_type            # top-level class hangs off Project
    assert ("~", "module/outer") in has_module        # top-level module hangs off Project
    assert ("module/outer", "class/outer.inner") in has_type    # nested class -> parent module
    assert ("module/outer", "module/outer.deep") in has_module   # nested module -> parent module
    classes = _nodes(nodes, "Class")
    assert classes["Inner"].properties["qualified_name"] == "Outer::Inner"


def test_reopened_class_merges_into_one_node():
    nodes, _ = _resolve({
        "a.rb": "class Store\n def a; end\nend",
        "b.rb": "class Store\n def b; end\nend",
    })
    stores = [n for n in nodes if n.label == "Class" and n.properties["name"] == "Store"]
    assert len(stores) == 1  # reopened across two files -> a single merged node
    methods = {n.properties["name"] for n in nodes if n.label == "Method"}
    assert {"a", "b"} <= methods


def test_members_hang_off_namespace():
    nodes, edges = _resolve({
        "app.rb": (
            "class Widget\n"
            "  TAU = 6\n"
            "  attr_reader :size\n"
            "  def area(w, h); end\n"
            "  def self.build; end\n"
            "end"
        ),
    })
    assert ("class/widget", "class/widget.method.area(w,h)") in _edges(edges, "HAS_METHOD")
    assert ("class/widget", "class/widget.field.size") in _edges(edges, "HAS_FIELD")
    assert ("class/widget", "class/widget.const.tau") in _edges(edges, "HAS_CONST")
    # parameters
    has_param = _edges(edges, "HAS_PARAMETER")
    assert any(f.startswith("class/widget.method.area") and t.endswith(".param.0.w") for f, t in has_param)
    methods = _nodes(nodes, "Method")
    assert methods["build"].properties["is_singleton"] is True
    assert methods["area"].properties["is_singleton"] is False


def test_top_level_def_is_a_function():
    nodes, edges = _resolve({"app.rb": "def run(x); end"})
    assert ("~", "~.func.run(x)") in _edges(edges, "HAS_FUNCTION")
    assert "run" in _nodes(nodes, "Function")


def test_extends_and_includes_project_namespaces_only():
    _, edges = _resolve({
        "app.rb": (
            "module Walkable\n end\n"
            "class Animal\n end\n"
            "class Dog < Animal\n include Walkable\n include Kernel\n end\n"  # Kernel not in project
        ),
    })
    assert ("class/dog", "class/animal") in _edges(edges, "EXTENDS")
    inc = _edges(edges, "INCLUDES")
    assert ("class/dog", "module/walkable") in inc
    assert inc[("class/dog", "module/walkable")]["kind"] == "include"
    # `include Kernel` names no project module -> no INCLUDES edge
    assert not any("kernel" in t for _, t in inc)


def test_imports_require_relative_and_require():
    _, edges = _resolve({
        "lib/dog.rb": 'require_relative "animal"\nrequire "app/util"',
        "lib/animal.rb": "class Animal; end",
        "app/util.rb": "def helper; end",
    })
    imp = _edges(edges, "IMPORTS")
    assert ("file/lib/dog.rb", "file/lib/animal.rb") in imp
    assert imp[("file/lib/dog.rb", "file/lib/animal.rb")]["resolution"] == "path"
    assert ("file/lib/dog.rb", "file/app/util.rb") in imp
    assert imp[("file/lib/dog.rb", "file/app/util.rb")]["resolution"] == "name"


def test_calls_command_self_super_and_toplevel():
    _, edges = _resolve({
        "app.rb": (
            "class Animal\n"
            "  def speak; end\n"
            "end\n"
            "class Dog < Animal\n"
            "  def speak\n"
            "    bark\n"          # command -> own method
            "    self.wag\n"      # self -> own method
            "    helper_fn\n"     # command -> top-level function
            "    super\n"         # super -> Animal#speak
            "  end\n"
            "  def bark; end\n"
            "  def wag; end\n"
            "end\n"
            "def helper_fn; end\n"
        ),
    })
    calls = _edges(edges, "CALLS")
    src = "class/dog.method.speak()"
    assert (src, "class/dog.method.bark()") in calls
    assert (src, "class/dog.method.wag()") in calls
    assert (src, "~.func.helper_fn()") in calls
    assert (src, "class/animal.method.speak()") in calls  # super
    assert all(p.get("resolution") == "name" for p in calls.values())


def test_calls_across_included_module_ancestor():
    # A receiverless call resolves through the include chain, not just the class's own methods.
    _, edges = _resolve({
        "app.rb": (
            "module Greetings\n def wave; end\nend\n"
            "class Dog\n include Greetings\n def act; wave; end\nend"
        ),
    })
    assert ("class/dog.method.act()", "module/greetings.method.wave()") in _edges(edges, "CALLS")


def test_constant_receiver_class_method_and_new():
    _, edges = _resolve({
        "app.rb": (
            "class Store\n"
            "  def initialize(x); end\n"
            "  def self.build; end\n"
            "end\n"
            "class Client\n"
            "  def run\n"
            "    Store.build\n"   # class method
            "    Store.new(1)\n"  # .new -> Store#initialize
            "  end\n"
            "end\n"
        ),
    })
    calls = _edges(edges, "CALLS")
    src = "class/client.method.run()"
    assert calls[(src, "class/store.method.build()#singleton")]["receiver"] == "Store"
    assert calls[(src, "class/store.method.initialize(x)")]["receiver"] == "Store"


def test_external_constant_call_is_placeholder():
    nodes, edges = _resolve({
        "app.rb": 'def f\n JSON.parse("{}")\n Acme::Client.connect\nend',
    })
    externs = {n.properties["qualified_name"]: n for n in nodes if n.properties.get("resolved") is False}
    assert externs["JSON.parse"].properties["origin"] == "stdlib"
    assert externs["JSON.parse"].key["id"] == "extern://ruby/JSON.parse"
    assert externs["Acme::Client.connect"].properties["origin"] == "external"
    calls = _edges(edges, "CALLS")
    assert calls[("~.func.f()", "extern://ruby/JSON.parse")]["resolution"] == "external"


def test_dynamic_instance_receiver_calls_are_dropped():
    # `x.foo` / `@iv.foo` have no static receiver type in a dynamic language -> no edge (no guess),
    # and no external placeholder (they aren't nameable).
    nodes, edges = _resolve({
        "app.rb": "class C\n def m(x)\n x.foo\n @iv.bar\n a.b.c\n end\nend",
    })
    assert [n for n in nodes if n.properties.get("resolved") is False] == []
    assert _edges(edges, "CALLS") == {}


def test_public_callables_carry_cross_repo_linkage_key():
    nodes, _ = _resolve({
        "app.rb": (
            "def compute; end\n"                    # top-level function
            "class Store\n"
            "  def put; end\n"                      # public instance method
            "  def self.make; end\n"                # class method
            "  private\n"
            "  def secret; end\n"                   # private -> not externally visible
            "end\n"
        ),
    })
    fns = _nodes(nodes, "Function")
    assert fns["compute"].properties["linkage_key"] == "extern://ruby/compute"
    methods = {(n.properties["name"], n.properties.get("is_singleton")): n for n in nodes if n.label == "Method"}
    assert methods[("put", False)].properties["linkage_key"] == "extern://ruby/Store#put"
    assert methods[("make", True)].properties["linkage_key"] == "extern://ruby/Store.make"
    assert "linkage_key" not in methods[("secret", False)].properties


def test_doc_property_reaches_nodes():
    nodes, _ = _resolve({
        "app.rb": (
            "# Store persists widgets.\n"
            "class Store\n"
            "  # put stores a value.\n"
            "  def put(v); end\n"
            "end\n"
            "# A greeter.\n"
            "module Greet\n end\n"
        ),
    })
    assert _nodes(nodes, "Class")["Store"].properties["doc"] == "Store persists widgets."
    assert _nodes(nodes, "Method")["put"].properties["doc"] == "put stores a value."
    assert _nodes(nodes, "Module")["Greet"].properties["doc"] == "A greeter."
