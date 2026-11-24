"""Offline tests for the Jedi engine -- the default deep engine.

Jedi runs in-process (pure-Python pip dep), so this needs no FalkorDB and no external
toolchain. It asserts the semantic upgrade over the ast engine: inferred variable/attribute
types, and receiver-typed call resolution (`conn.execute` -> `Connection.execute`), all with
honest provenance. This is the payoff of choosing Jedi over both a plain ast parser and an
out-of-process type checker.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.ingestors.python import resolution
from auryn.ingestors.python.file_parser import parse_module
from auryn.ingestors.python.jedi_engine import JediOracle

_SAMPLE = Path(__file__).resolve().parents[3] / "demo" / "python-ingestor" / "sample"
_PROJ = "python://demo"


@pytest.fixture
def items():
    files = sorted(_SAMPLE.rglob("*.py"))
    modules = [parse_module(f.read_bytes(), str(f.relative_to(_SAMPLE))) for f in files]
    oracle = JediOracle(str(_SAMPLE))
    return list(resolution.resolve(modules, _PROJ, oracle=oracle))


def _nodes(items):
    return [i for i in items if isinstance(i, GraphNode)]


def _edges_of(items, rel):
    return [i for i in items if isinstance(i, GraphEdge) and i.rel_type == rel]


def _node(items, node_id):
    for n in _nodes(items):
        if n.key["id"] == node_id:
            return n
    raise AssertionError(f"no node {node_id}")


def test_attribute_types_are_inferred(items):
    def attr(cid, name):
        return _node(items, f"{cid}.attr.{name}").properties

    widget = "python://demo/module/app.widget.class.widget"
    entity = "python://demo/module/app.models.class.entity"
    assert (attr(entity, "id")["type"], attr(entity, "id")["type_source"]) == ("int", "inferred")
    assert (attr(widget, "name")["type"], attr(widget, "name")["type_source"]) == ("str", "inferred")
    assert attr(widget, "tags")["type_source"] == "inferred"  # list


def test_receiver_typed_call_resolves_as_inferred(items):
    calls = {
        str(e.to_key["id"]): e.properties
        for e in _edges_of(items, "CALLS")
        if e.from_key["id"] == "python://demo/module/app.widget.class.widget.method.render(self)"
    }
    execute = calls["python://demo/module/app.db.class.connection.method.execute(self,sql)"]
    assert execute["resolution"] == "inferred"
    assert execute["receiver_type"] == "Connection"
    # the direct import-bound call stays "resolved"
    assert calls["python://demo/module/app.db.func.get_connection()"]["resolution"] == "resolved"


def test_self_param_type_is_inferred_to_the_class(items):
    self_p = _node(items, "python://demo/module/app.widget.class.widget.method.render(self).param.0.self")
    assert self_p.properties["type"] == "Widget"
    assert self_p.properties["type_source"] == "inferred"


def test_unannotated_param_inferred_from_call_site(items):
    # Widget.__init__'s `id` has no annotation, but Jedi infers `int` from the call site
    # `Widget(widget_id, ...)` in app/api.py where `widget_id: int` -- cross-file call-site
    # inference a plain ast parser can't do (the ast engine leaves this `unresolved`).
    p = _node(items, "python://demo/module/app.widget.class.widget.method.__init__(self,id,name).param.1.id")
    assert p.properties["type"] == "int"
    assert p.properties["type_source"] == "inferred"


def test_provenance_covers_annotated_and_inferred(items):
    # On this fully-connected sample Jedi resolves every type, so only annotated + inferred
    # appear (`unresolved` is exercised by the ast engine, which never infers).
    sources = {
        n.properties["type_source"]
        for n in _nodes(items)
        if n.label in ("Field", "Parameter")
    }
    assert sources == {"annotated", "inferred"}


def test_external_calls_become_placeholder_nodes(items):
    # ADR 0004 (reverses the old drop-behavior): json.dumps (stdlib) and getattr (builtin) now
    # surface as placeholder Function nodes (resolved=false + origin), reached by ordinary CALLS
    # edges -- so "what calls json.dumps / getattr / eval" is a one-hop query even though the
    # oracle bound them outside the project.
    render = "python://demo/module/app.widget.class.widget.method.render(self)"
    ext = {str(e.to_key["id"]) for e in _edges_of(items, "CALLS")
           if e.from_key["id"] == render and e.properties["resolution"] == "external"}
    assert "extern://python/json::dumps" in ext
    assert "extern://python/builtins::getattr" in ext

    dumps = _node(items, "extern://python/json::dumps")
    assert dumps.label == "Function"
    assert dumps.properties["resolved"] is False
    assert dumps.properties["origin"] == "stdlib"
    assert dumps.properties["linkage_key"] == "extern://python/json::dumps"
    assert _node(items, "extern://python/builtins::getattr").properties["origin"] == "builtin"

    # a still-dynamic call (the result of getattr(...)() ) remains unnameable -> no edge
    assert not any("execute" in str(e.to_key["id"]) and e.properties["resolution"] == "external"
                   for e in _edges_of(items, "CALLS"))


# -- instantiation -> constructor (the deep engine, end-to-end over a materialized dir) ---------


def test_instantiation_resolves_to_constructor(tmp_path):
    """`Service()` -- at module scope and inside a function -- edges to the class's __init__ with
    resolution `constructor`; a class with no __init__ draws no such edge."""
    (tmp_path / "app.py").write_text(
        "class Service:\n"
        "    def __init__(self):\n"
        "        self.ready = True\n"
        "    def run(self):\n"
        "        pass\n"
        "\n"
        "class Bare:\n"
        "    def run(self):\n"
        "        pass\n"
        "\n"
        "def boot():\n"
        "    Service()\n"
        "\n"
        "Service()\n"
        "Bare()\n"
    )
    m = parse_module((tmp_path / "app.py").read_bytes(), "app.py")
    items = list(resolution.resolve([m], _PROJ, oracle=JediOracle(str(tmp_path))))

    init_id = "python://demo/module/app.class.service.method.__init__(self)"
    ctor = [e for e in _edges_of(items, "CALLS")
            if e.to_key["id"] == init_id and e.properties["resolution"] == "constructor"]
    froms = {e.from_key["id"] for e in ctor}
    assert "python://demo/module/app" in froms            # module-scope `Service()`
    assert "python://demo/module/app.func.boot()" in froms  # in-function `Service()`
    assert all(e.properties["receiver_type"] == "Service" for e in ctor)

    # Bare has no __init__ -> no edge to a __init__ METHOD (it now edges to the Class node instead, #785)
    assert not any(str(e.to_key["id"]).endswith("bare.method.__init__()") for e in _edges_of(items, "CALLS"))


def test_collect_queries_includes_module_scope_calls():
    # a top-level (module-scope) call -- e.g. a module-level lambda's body -- must be in the jedi query
    # set, else the precomputed oracle returns no target for it during deep ingestion (#785 review)
    from auryn.ingestors.python.jedi_engine import _collect_queries

    m = parse_module(b"def helper():\n    return 1\n\nx = helper()\n", "m.py")
    assert m.calls  # the parser captured the module-scope call
    _types, call_positions = _collect_queries(m)
    assert (m.calls[0].line, m.calls[0].col) in call_positions
