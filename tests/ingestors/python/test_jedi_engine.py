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
from auryn.ingestors.python import ids, resolution
from auryn.ingestors.python.file_parser import parse_module
from auryn.ingestors.python.jedi_engine import JediOracle, _callee_name, build_precomputed_oracle

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
        if n.label in ("Attribute", "Parameter")
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


# -- src/ layout: a callee must be named the way the ast pass named the file defining it --------


def _materialize(root, files):
    """Write `files` under `root` and parse them the way _jedi_sync_at does (ingest-wide package
    context, sorted paths)."""
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    pkg_dirs = ids.package_dirs_from_paths(files)
    return [parse_module(text, rel, pkg_dirs) for rel, text in sorted(files.items())]


@pytest.mark.parametrize("oracle_kind", ["inline", "precomputed"])
def test_src_layout_calls_bind_to_project_callables(tmp_path, oracle_kind):
    """Jedi names `src/pkg/db.py` by its path under the project root (`src.pkg.db`); the ast pass
    anchors it at its import root (`pkg.db`). A same-module, relative-import or receiver-typed
    callee must still land on the project node -- not degrade to an `extern://python/src.pkg...`
    placeholder -- through both oracles (the precomputed one is what _jedi_sync_at runs)."""
    modules = _materialize(tmp_path, {
        "src/pkg/__init__.py": "",
        "src/pkg/db.py": (
            "class Connection:\n"
            "    def execute(self, sql):\n"
            "        return sql\n"
            "\n"
            "\n"
            "def get_connection():\n"
            "    return Connection()\n"
        ),
        "src/pkg/api.py": (
            "from .db import get_connection\n"
            "\n"
            "\n"
            "def _sql():\n"
            "    return 'select 1'\n"
            "\n"
            "\n"
            "def run():\n"
            "    conn = get_connection()\n"
            "    return conn.execute(_sql())\n"
        ),
    })
    if oracle_kind == "inline":
        oracle = JediOracle(str(tmp_path), modules)
    else:
        oracle = build_precomputed_oracle(str(tmp_path), modules, workers=1)
    items = list(resolution.resolve(modules, _PROJ, oracle=oracle))

    calls = {
        str(e.to_key["id"]): e.properties
        for e in _edges_of(items, "CALLS")
        if e.from_key["id"] == "python://demo/module/pkg.api.func.run()"
    }
    # the same-module and the `from .db` callee are bound by name -> "resolved"
    assert calls["python://demo/module/pkg.api.func._sql()"]["resolution"] == "resolved"
    assert calls["python://demo/module/pkg.db.func.get_connection()"]["resolution"] == "resolved"
    # the receiver-typed call still reaches the method, as "inferred"
    execute = calls["python://demo/module/pkg.db.class.connection.method.execute(self,sql)"]
    assert (execute["resolution"], execute["receiver_type"]) == ("inferred", "Connection")
    # nothing here is defined outside the project -> not one placeholder
    assert not [e for e in _edges_of(items, "CALLS") if e.properties["resolution"] == "external"]


def test_callee_name_is_left_alone_when_jedi_names_it_as_the_importer_spelled_it():
    """Jedi stopped at an import inside a project file (a re-export of something it can't follow):
    the name is the import path, not `<module>.<name>` -- there is no module prefix to re-anchor."""
    by_file = {"/repo/src/pkg/__init__.py": "pkg"}
    assert _callee_name("src.pkg.boot", "src.pkg", "/repo/src/pkg/__init__.py", by_file) == "pkg.boot"
    assert _callee_name("lib.fetch", "src.pkg", "/repo/src/pkg/__init__.py", by_file) == "lib.fetch"
    assert _callee_name("src.pkgs.fetch", "src.pkg", "/repo/src/pkg/__init__.py", by_file) == "src.pkgs.fetch"


def test_callee_in_a_namespace_dir_binds_by_file(tmp_path):
    """`plugins/` is a PEP-420 namespace dir (no __init__.py), so the ast pass keeps the
    context-free name for its modules (`src.pkg.plugins.hooks`) while Jedi follows the import and
    names the callee `pkg.plugins.hooks.fire`. Matching the callee on the *file* that defines it
    binds it to whatever dotted name that file was indexed under."""
    modules = _materialize(tmp_path, {
        "src/pkg/__init__.py": "",
        "src/pkg/plugins/hooks.py": "def fire():\n    return 1\n",
        "src/pkg/api.py": "from pkg.plugins.hooks import fire\n\n\ndef run():\n    return fire()\n",
    })
    hooks = next(m for m in modules if m.path == "src/pkg/plugins/hooks.py")
    items = list(resolution.resolve(modules, _PROJ, oracle=JediOracle(str(tmp_path), modules)))

    targets = {str(e.to_key["id"]) for e in _edges_of(items, "CALLS")
               if e.from_key["id"] == "python://demo/module/pkg.api.func.run()"}
    assert targets == {f"python://demo/module/{hooks.dotted}.func.fire()"}


@pytest.mark.parametrize("oracle_kind", ["inline", "precomputed"])
def test_call_jedi_cannot_follow_falls_back_to_the_ast_rules(tmp_path, oracle_kind):
    """Jedi can't follow `from pkg.core import helper` out of tests/ (`src/` isn't on its path), nor
    an import of a library that isn't installed here, and answers nothing for those calls. They
    must still bind the way the fast pass binds them -- by the import -- not vanish from the graph."""
    modules = _materialize(tmp_path, {
        "src/pkg/__init__.py": "",
        "src/pkg/core.py": "def helper():\n    return 1\n",
        "tests/test_core.py": (
            "from notinstalled_lib import fetch\n"
            "from pkg import core\n"
            "from pkg import core as rebound\n"
            "from pkg.core import helper\n"
            "\n"
            "\n"
            "def test_it():\n"
            "    fetch(helper())\n"
            "\n"
            "\n"
            "def test_shadowed(fetch, helper):\n"
            "    fetch(helper())\n"
            "\n"
            "\n"
            "def test_shadowed_by_a_local(make):\n"
            "    core = make()\n"
            "    core.helper()\n"
            "\n"
            "\n"
            "def test_shadowed_by_an_except_name():\n"
            "    try:\n"
            "        pass\n"
            "    except ValueError as core:\n"
            "        core.helper()\n"
            "\n"
            "\n"
            "def test_shadowed_by_a_local_import():\n"
            "    from notinstalled_lib import core\n"
            "    core.helper()\n"
            "\n"
            "\n"
            "def test_shadowed_by_the_module():\n"
            "    rebound.helper()\n"
            "\n"
            "\n"
            "rebound = object()  # its own name: the function-local cases above rest on their own guards\n"
            "rebound.helper()\n"
        ),
    })
    oracle = JediOracle(str(tmp_path), modules) if oracle_kind == "inline" else build_precomputed_oracle(str(tmp_path), modules, workers=1)
    items = list(resolution.resolve(modules, _PROJ, oracle=oracle))

    calls = {str(e.to_key["id"]): e.properties["resolution"] for e in _edges_of(items, "CALLS")
             if e.from_key["id"] == "python://demo/module/tests.test_core.func.test_it()"}
    assert calls == {
        "python://demo/module/pkg.core.func.helper()": "resolved",
        "extern://python/notinstalled_lib::fetch": "external",
    }
    # ...but not where the scope's own parameter or local (an assignment, `except ... as`, an
    # import in the body) shadows the import: that call IS dynamic. Nor where the top-level code
    # rebinds the name -- at module scope, or in a function that calls through that global.
    assert not [e for e in _edges_of(items, "CALLS") if "test_shadowed" in str(e.from_key["id"])]
    top_level = {str(e.to_key["id"]) for e in _edges_of(items, "CALLS")
                 if e.from_key["id"] == "python://demo/module/tests.test_core"}
    assert [callee.rsplit("::", 1)[-1] for callee in top_level] == ["object"]  # `object()`, nothing else


def test_fallback_steps_aside_for_a_name_the_module_binds_twice(tmp_path):
    """The ast rules don't look at order: `def helper` followed by `from missing_lib import helper`
    reads as the module's own function, and `from pkg import core` wins over a `from otherlib
    import core` under an `if`. Where Jedi found nothing the deep pass has no edge to add there:
    a name with two module-level bindings stays unresolved, as on main."""
    modules = _materialize(tmp_path, {
        "src/pkg/__init__.py": "",
        "src/pkg/core.py": "def helper():\n    return 1\n",
        "tests/test_core.py": (
            "import sys\n"
            "from pkg import core\n"
            "\n"
            "\n"
            "def helper():\n"
            "    return 0\n"
            "\n"
            "\n"
            "from missing_lib import helper\n"
            "\n"
            "if sys.platform == 'win32':\n"
            "    from otherlib import core\n"
            "\n"
            "\n"
            "def test_it():\n"
            "    helper()\n"
            "    core.helper()\n"
        ),
    })
    items = list(resolution.resolve(modules, _PROJ, oracle=JediOracle(str(tmp_path), modules)))

    assert not [e for e in _edges_of(items, "CALLS")
                if e.from_key["id"] == "python://demo/module/tests.test_core.func.test_it()"]


def test_collect_queries_includes_module_scope_calls():
    # a top-level (module-scope) call -- e.g. a module-level lambda's body -- must be in the jedi query
    # set, else the precomputed oracle returns no target for it during deep ingestion (#785 review)
    from auryn.ingestors.python.jedi_engine import _collect_queries

    m = parse_module(b"def helper():\n    return 1\n\nx = helper()\n", "m.py")
    assert m.calls  # the parser captured the module-scope call
    _types, call_positions = _collect_queries(m)
    assert (m.calls[0].line, m.calls[0].col) in call_positions
