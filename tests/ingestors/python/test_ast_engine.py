"""Offline tests for the stdlib-`ast` engine (file_parser + resolution, no oracle).

Runs the engine over the demo sample source with no FalkorDB and no third-party deps, and
asserts what static analysis honestly can and cannot do: it resolves cross-file EXTENDS +
MRO and an import-bound call, but its types are only `annotated`/`unresolved` (never
`inferred`) and it cannot reach the receiver-typed `conn.execute()` call. The Jedi engine
(test_jedi_engine.py) upgrades exactly those gaps.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.ingestors.python import resolution
from auryn.ingestors.python.file_parser import parse_module

_SAMPLE = Path(__file__).resolve().parents[3] / "demo" / "python-ingestor" / "sample"
_PROJ = "python://demo"


def test_syntax_error_reports_source_path(caplog):
    module = parse_module("print 'Python 2'\n", "deps/legacy.py")
    assert module.parse_ok is False
    assert "legacy.py, line 1" in caplog.text
    assert "<unknown>" not in caplog.text


def test_syntax_warning_reports_source_path():
    with pytest.warns(SyntaxWarning, match="invalid escape sequence") as caught:
        module = parse_module("pattern = '\\s'\n", "src/patterns.py")
    assert module.parse_ok is True
    assert caught[0].filename == "src/patterns.py"
    assert caught[0].lineno == 1


def _resolve(oracle=None):
    files = sorted(_SAMPLE.rglob("*.py"))
    modules = [parse_module(f.read_bytes(), str(f.relative_to(_SAMPLE))) for f in files]
    return list(resolution.resolve(modules, _PROJ, oracle=oracle))


@pytest.fixture
def items():
    return _resolve()


def _nodes(items):
    return [i for i in items if isinstance(i, GraphNode)]


def _edges_of(items, rel):
    return [i for i in items if isinstance(i, GraphEdge) and i.rel_type == rel]


def _node(items, node_id):
    for n in _nodes(items):
        if n.key["id"] == node_id:
            return n
    raise AssertionError(f"no node {node_id}")


def test_backbone_ids_follow_the_scheme(items):
    ids = {str(n.key["id"]) for n in _nodes(items)}
    assert "python://demo/package/app" in ids
    assert "python://demo/module/app.widget" in ids
    assert "python://demo/module/app.widget.class.widget" in ids
    # method id keys on param NAMES, never types (id-stability rule)
    assert "python://demo/module/app.widget.class.widget.method.render(self)" in ids
    assert "python://demo/module/app.db.func.get_connection()" in ids


def test_cross_file_inheritance_resolves_with_mro(items):
    widget = _node(items, "python://demo/module/app.widget.class.widget")
    assert widget.properties["mro"] == ["app.widget.Widget", "app.models.Entity", "object"]
    extends = _edges_of(items, "EXTENDS")
    assert len(extends) == 1
    assert extends[0].to_key["id"] == "python://demo/module/app.models.class.entity"
    assert extends[0].properties == {"resolution": "resolved", "mro_index": 1}


def test_only_import_bound_calls_resolve_and_never_inferred(items):
    targets = [str(e.to_key["id"]) for e in _edges_of(items, "CALLS")]
    assert "python://demo/module/app.db.func.get_connection()" in targets  # import-bound
    assert not any("execute" in t for t in targets)  # receiver-typed: ast can't reach it
    resolutions = {e.properties["resolution"] for e in _edges_of(items, "CALLS")}
    # ast: project calls are "resolved"; an instantiation is "constructor" (statically bound to the
    # class's __init__); unresolved-but-nameable ones (stdlib/builtin) are "external" placeholders
    # (ADR 0004). Never "inferred" -- that value is the Jedi engine's signal.
    assert resolutions <= {"resolved", "constructor", "external"}
    assert "inferred" not in resolutions


def test_types_are_only_annotated_or_unresolved(items):
    sources = set()
    for n in _nodes(items):
        if n.properties.get("resolved") is False:
            continue  # ADR 0004 placeholders are shallow (no type provenance)
        if n.label in ("Attribute", "Parameter"):
            sources.add(n.properties["type_source"])
        if n.label in ("Method", "Function"):
            sources.add(n.properties["return_type_source"])
    assert sources <= {"annotated", "unresolved"}
    assert "inferred" not in sources


def test_method_qualified_name_includes_its_class(items):
    render = _node(items, "python://demo/module/app.widget.class.widget.method.render(self)")
    assert render.properties["qualified_name"] == "app.widget.Widget.render"


def test_external_refs_surface_as_placeholder_nodes(items):
    # ADR 0004: stdlib/builtin/third-party references the resolver can name become placeholder
    # nodes (resolved=false + origin), reached by ordinary CALLS/IMPORTS edges -- not dropped.
    render = "python://demo/module/app.widget.class.widget.method.render(self)"
    ext_calls = {str(e.to_key["id"]) for e in _edges_of(items, "CALLS")
                 if e.from_key["id"] == render and e.properties["resolution"] == "external"}
    assert {"extern://python/json::dumps", "extern://python/builtins::getattr"} <= ext_calls

    dumps = _node(items, "extern://python/json::dumps")
    assert (dumps.label, dumps.properties["resolved"], dumps.properties["origin"]) == ("Function", False, "stdlib")
    assert dumps.properties["linkage_key"] == "extern://python/json::dumps"  # == a real definer's key
    assert _node(items, "extern://python/builtins::getattr").properties["origin"] == "builtin"

    # external imports keep the "came from module B" provenance as placeholder Module nodes
    ext_imports = {str(e.to_key["id"]) for e in _edges_of(items, "IMPORTS")
                   if e.properties["resolution"] == "external"}
    assert {"extern://python/json", "extern://python/fastapi"} <= ext_imports
    assert _node(items, "extern://python/fastapi").properties["origin"] == "external"  # third-party


def test_real_callables_carry_a_linkage_key(items):
    # A real definition's `linkage_key` is the project-independent key a consumer in another repo
    # would compute -- the hinge that lets a later ingest upgrade a placeholder in place.
    get_conn = _node(items, "python://demo/module/app.db.func.get_connection()")
    assert get_conn.properties["linkage_key"] == "extern://python/app.db::get_connection"
    widget = _node(items, "python://demo/module/app.widget.class.widget")
    assert widget.properties["linkage_key"] == "extern://python/app.widget::Widget"


# -- doc comments (shared `doc` property via core.docstrings.leading_comment_from_source) ---


def _module(src: str):
    return parse_module(src.encode(), "m.py")


def test_leading_hash_comment_on_function():
    m = _module(
        "\n".join(
            [
                "# Connect to the database.",
                "# Retries on failure.",
                "def connect():",
                "    return 1",
            ]
        )
    )
    (fn,) = m.functions
    assert fn.doc == "Connect to the database.\nRetries on failure."


def test_leading_comment_sits_above_decorators():
    m = _module(
        "\n".join(
            [
                "# Cache the result.",
                "@lru_cache",
                "@staticmethod",
                "def compute():",
                "    return 2",
            ]
        )
    )
    (fn,) = m.functions
    assert fn.doc == "Cache the result."


def test_class_doc_and_method_doc():
    m = _module(
        "\n".join(
            [
                "# Widget renders things.",
                "class Widget:",
                "    # Draw the widget.",
                "    def draw(self):",
                "        ...",
            ]
        )
    )
    (cls,) = m.classes
    assert cls.doc == "Widget renders things."
    (method,) = cls.methods
    assert method.doc == "Draw the widget."


def test_no_leading_comment_is_empty():
    m = _module("def bare():\n    return 0\n")
    assert m.functions[0].doc == ""


def test_doc_property_reaches_resolution_nodes():
    m = _module(
        "\n".join(
            [
                "# A top-level helper.",
                "def helper():",
                "    ...",
            ]
        )
    )
    nodes = _nodes(list(resolution.resolve([m], "python://demo")))
    fn = next(n for n in nodes if n.label == "Function" and n.properties.get("name") == "helper")
    assert fn.properties["doc"] == "A top-level helper."


# -- instantiation -> constructor (a call resolving to a project class edges to its __init__) ---


def _calls_from(items, src_id):
    return {
        (e.to_key["id"], e.properties["resolution"])
        for e in _edges_of(items, "CALLS")
        if e.from_key["id"] == src_id
    }


def test_same_module_instantiation_edges_to_constructor():
    m = _module(
        "\n".join(
            [
                "class Service:",
                "    def __init__(self):",
                "        self.ready = True",
                "    def run(self):",
                "        pass",
                "",
                "def boot():",
                "    Service()",  # in-function instantiation
                "",
                "Service()",  # module-level (top-level) instantiation
            ]
        )
    )
    items = list(resolution.resolve([m], "python://demo"))
    init_id = "python://demo/module/m.class.service.method.__init__(self)"
    # both the module node and the function node edge to __init__, tagged `constructor`
    assert (init_id, "constructor") in _calls_from(items, "python://demo/module/m")
    assert (init_id, "constructor") in _calls_from(items, "python://demo/module/m.func.boot()")
    ctor_edge = next(
        e for e in _edges_of(items, "CALLS")
        if e.to_key["id"] == init_id and e.from_key["id"] == "python://demo/module/m"
    )
    assert ctor_edge.properties["receiver_type"] == "Service"


def test_instantiation_without_init_draws_no_edge():
    # A class with no local __init__ has no node to point at -> no constructor edge (and no stray
    # external placeholder either): a default/inherited ctor keeps the no-edge convention.
    m = _module("class Bare:\n    def run(self):\n        pass\n\nBare()\n")
    items = list(resolution.resolve([m], "python://demo"))
    assert [e for e in _edges_of(items, "CALLS") if e.properties["resolution"] == "constructor"] == []
    assert _calls_from(items, "python://demo/module/m") == set()


def test_import_bound_instantiation_resolves_to_constructor():
    svc = parse_module(b"class Service:\n    def __init__(self, x):\n        self.x = x\n", "svc.py")
    main = parse_module(b"from svc import Service\n\nService(1)\n", "main.py")
    items = list(resolution.resolve([svc, main], "python://demo"))
    init_id = "python://demo/module/svc.class.service.method.__init__(self,x)"
    assert (init_id, "constructor") in _calls_from(items, "python://demo/module/main")


# -- scope-correct call extraction: a scope owns only the calls its own code makes --------------


def test_decorator_call_is_not_attributed_to_the_decorated_function():
    # `@deco()` executes in the enclosing scope at def time -- the decorated function does NOT
    # "call" its decorator. (Regression: ast.walk used to sweep the decorator_list into _calls.)
    m = _module("def deco(f):\n    return f\n\n@deco()\ndef handler():\n    pass\n")
    items = list(resolution.resolve([m], "python://demo"))
    assert _calls_from(items, "python://demo/module/m.func.handler()") == set()


def test_nested_def_body_calls_do_not_fold_into_the_enclosing_function():
    # A call inside a nested def runs when the nested def runs -- it is NOT a call of the outer
    # function. (Regression: ast.walk used to fold the inner body's calls onto the enclosing def.)
    m = _module(
        "def target():\n    pass\n\ndef outer():\n    def inner():\n        target()\n    return inner\n"
    )
    items = list(resolution.resolve([m], "python://demo"))
    assert _calls_from(items, "python://demo/module/m.func.outer()") == set()


def test_local_names_are_the_names_a_body_binds_itself():
    # What the jedi engine's fallback must not bind through (see resolution._resolve_call): every
    # way a body binds a name, an `import` in it included -- but not its parameters (kept apart),
    # a nested scope's own names, or a name it only reads.
    m = _module(
        "def f(param, items):\n"
        "    a = b, (c, *d) = param\n"
        "    for e in items:\n"
        "        pass\n"
        "    with open(a) as g:\n"
        "        pass\n"
        "    try:\n"
        "        pass\n"
        "    except ValueError as h:\n"
        "        pass\n"
        "    if (i := len(items)):\n"
        "        pass\n"
        "    match param:\n"
        "        case [j, *k]:\n"
        "            pass\n"
        "        case {'key': 1, **l}:\n"
        "            pass\n"
        "    m = [n for n in items]\n"
        "    def nested(inner_param):\n"
        "        inner_local = 1\n"
        "    class Local:\n"
        "        attr = 1\n"
        "    import json\n"
        "    from os import path as p, sep\n"
        "    print(unbound)\n"
    )
    assert set(m.functions[0].local_names) == set("abcdeghijklmnp") | {"nested", "Local", "json", "sep"}


def test_module_local_names_are_only_what_the_top_level_code_rebinds():
    # At module scope a def, a class and an import are the module's own names -- what a top-level
    # call resolves to. Only a rebinding shadows one: `db = make()` after `from pkg import db`.
    m = _module(
        "import json\n"
        "from pkg import db\n"
        "\n"
        "def helper():\n"
        "    local = 1\n"
        "\n"
        "class K:\n"
        "    attr = 1\n"
        "\n"
        "db = make()\n"
        "for i in range(3):\n"
        "    helper()\n"
    )
    assert m.local_names == ["db", "i"]
