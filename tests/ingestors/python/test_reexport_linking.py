"""Cross-repo links through package re-exports and overloads (agent-evidence design §6).

A consumer writes `from lib import Foo` -- the public re-export spelling -- while `Foo` is defined
in `lib.sub.mod`. Linkage is an exact match on keys, so without aliases the consumer's
`extern://python/lib::Foo` placeholder never meets the definer's `extern://python/lib.sub.mod::Foo`.
These ingest two real projects into ONE graph, in both orders, and check the call reaches the
definition."""

from __future__ import annotations

from pathlib import Path

import pytest

from auryn.core.graph_client import GraphWriter
from auryn.core.ingest import ingest_directory

_LIB_FILES = {
    "lib/__init__.py": (
        "from typing import TYPE_CHECKING\n"
        "from .sub.mod import Foo\n"
        "from .fields import field\n"
        "if TYPE_CHECKING:\n"
        "    from .typed import Typed\n"
        "_dynamic_imports = {'Lazy': (__spec__.parent, '.lazy')}\n"
        "def __getattr__(name):\n"
        "    raise AttributeError(name)\n"
    ),
    "lib/sub/__init__.py": "",
    "lib/sub/mod.py": "class Foo:\n    def __init__(self):\n        self.x = 1\n",
    "lib/typed.py": "class Typed:\n    pass\n",
    "lib/lazy.py": "def Lazy():\n    return 1\n",
    "lib/fields.py": (
        "from typing import overload\n"
        "@overload\n"
        "def field(number: int) -> int: ...\n"
        "@overload\n"
        "def field(text: str) -> str: ...\n"
        "def field(value):\n"
        "    return value\n"
    ),
}

_APP_FILES = {
    "app/__init__.py": "",
    "app/main.py": (
        "from lib import Foo, field, Typed, Lazy\n"
        "def run():\n"
        "    Foo()\n"
        "    field(1)\n"
        "    Typed()\n"
        "    Lazy()\n"
    ),
}


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return root


def _ingest(graph, path: Path, project: str) -> None:
    ingest_directory(GraphWriter(graph), str(path), project_name=project, deep="off", auto_install=False)


def _call_targets(graph) -> dict[str, tuple[str, object]]:
    rows = graph.ro_query(
        "MATCH (c:Function {name: 'run'})-[:CALLS]->(t) RETURN t.name, t.id, t.resolved"
    ).result_set
    return {name: (tid, resolved) for name, tid, resolved in rows}


@pytest.mark.parametrize("order", ["definer_first", "consumer_first"])
def test_reexported_names_link_to_their_real_definitions(graph, tmp_path, order):
    lib = _write(tmp_path / "libroot", _LIB_FILES)
    app = _write(tmp_path / "approot", _APP_FILES)
    pairs = [(lib, "libproj"), (app, "appproj")]
    for path, project in (pairs if order == "definer_first" else pairs[::-1]):
        _ingest(graph, path, project)

    targets = _call_targets(graph)
    for name in ("Foo", "Typed", "Lazy", "field"):
        tid, resolved = targets[name]
        assert not tid.startswith("extern://"), f"{name} still points at a placeholder: {tid}"
        assert resolved is not False
    # the overload implementation (not a stub) is the link target
    impl = graph.ro_query(
        "MATCH (f:Function {name: 'field'}) WHERE f.decorators IS NULL OR NOT 'overload' IN f.decorators "
        "RETURN f.id"
    ).result_set[0][0]
    assert targets["field"][0] == impl
    stubs = graph.ro_query("MATCH (f:Function {name: 'field'}) RETURN count(f)").result_set[0][0]
    assert stubs == 3  # two @overload stubs + the implementation are distinct nodes
    # no dangling re-export placeholders remain
    left = graph.ro_query(
        "MATCH (n) WHERE n.id IN ['extern://python/lib::Foo', 'extern://python/lib::Typed', "
        "'extern://python/lib::Lazy', 'extern://python/lib::field'] RETURN count(n)"
    ).result_set[0][0]
    assert left == 0


def test_exact_definition_wins_over_an_alias(graph, tmp_path):
    # lib/__init__ DEFINES Foo itself AND re-exports a different Foo-named symbol under another
    # name; the consumer's `lib::Foo` must link to the exact definition, never through an alias.
    lib = _write(tmp_path / "libroot", {
        "lib/__init__.py": "from .other import Bar as Foo2\nclass Foo:\n    pass\n",
        "lib/other.py": "class Bar:\n    pass\n",
    })
    app = _write(tmp_path / "approot", {"app/main.py": "from lib import Foo\ndef run():\n    Foo()\n"})
    _ingest(graph, app, "appproj")
    _ingest(graph, lib, "libproj")
    tid, _ = _call_targets(graph)["Foo"]
    exact = graph.ro_query("MATCH (c:Class {name: 'Foo'}) RETURN c.id").result_set[0][0]
    assert tid == exact


def test_package_node_records_its_reexports(graph, tmp_path):
    lib = _write(tmp_path / "libroot", _LIB_FILES)
    _ingest(graph, lib, "libproj")
    table = graph.ro_query("MATCH (p:Package {dotted_path: 'lib'}) RETURN p.reexports").result_set[0][0]
    assert "Foo=extern://python/lib.sub.mod::Foo" in table
    assert "Typed=extern://python/lib.typed::Typed" in table
    assert "Lazy=extern://python/lib.lazy::Lazy" in table
    no_table = graph.ro_query("MATCH (p:Package {dotted_path: 'lib.sub'}) RETURN p.reexports").result_set[0][0]
    assert no_table is None  # a package with no re-exports keeps its old property set


def test_reingest_after_edits_moves_lines_and_keeps_the_link(graph, tmp_path):
    lib = _write(tmp_path / "libroot", _LIB_FILES)
    app = _write(tmp_path / "approot", _APP_FILES)
    _ingest(graph, lib, "libproj")
    _ingest(graph, app, "appproj")

    # shift the Foo() call down two lines, and edit the definer module (re-emits Foo)
    (app / "app/main.py").write_text(_APP_FILES["app/main.py"].replace("def run():\n", "def run():\n    a = 1\n    b = 2\n"))
    (lib / "lib/sub/mod.py").write_text(_LIB_FILES["lib/sub/mod.py"] + "\n\ndef helper():\n    return 2\n")
    _ingest(graph, app, "appproj")
    _ingest(graph, lib, "libproj")

    rows = graph.ro_query(
        "MATCH (c:Function {name: 'run'})-[r:CALLS]->(t {name: 'Foo'}) RETURN t.id, r.lines"
    ).result_set
    assert len(rows) == 1, "a moved call must update its edge, not add a second one"
    tid, lines = rows[0]
    assert lines == [5] and not tid.startswith("extern://")


def test_two_spellings_of_one_symbol_keep_every_call_line(graph, tmp_path):
    # `from lib import Foo` (alias) and `from lib.sub.mod import Foo as F2` (exact) in one caller:
    # two placeholders fold onto ONE edge in the same reconcile pass -- their lines must union.
    lib = _write(tmp_path / "libroot", _LIB_FILES)
    app = _write(tmp_path / "approot", {"app/main.py": (
        "from lib import Foo\nfrom lib.sub.mod import Foo as F2\ndef run():\n    F2()\n    x = 1\n    Foo()\n"
    )})
    _ingest(graph, app, "appproj")
    _ingest(graph, lib, "libproj")
    rows = graph.ro_query(
        "MATCH (c:Function {name: 'run'})-[r:CALLS]->(t:Class {name: 'Foo'}) RETURN r.lines"
    ).result_set
    assert len(rows) == 1
    assert sorted(rows[0][0]) == [4, 6]


# -- incremental resync == full recompute when an __init__'s re-exports change ------------------

def _git_repo(root: Path, files: dict[str, str]):
    import git

    _write(root, files)
    repo = git.Repo.init(str(root), initial_branch="main")
    with repo.config_writer() as cw:
        cw.set_value("user", "email", "t@example.com")
        cw.set_value("user", "name", "T")
    repo.git.add(A=True)
    repo.index.commit("init")
    return repo


def _reexports(graph) -> list:
    rows = graph.ro_query("MATCH (p:Package) RETURN p.dotted_path, p.reexports ORDER BY p.dotted_path").result_set
    return [(d, sorted(r) if r else None) for d, r in rows]


@pytest.mark.parametrize("new_init", [
    "from .sub.mod import Foo, bar\n",  # a re-export added
    "x = 1\n",                           # every re-export removed
])
def test_incremental_reexport_change_matches_full_recompute(falkordb_connection, tmp_path, new_init):
    from uuid import uuid4

    from auryn.ingestors.python.git_sync import GitPythonProjectConfig, sync

    root = tmp_path / "lib"
    repo = _git_repo(root, {
        "lib/__init__.py": "from .sub.mod import Foo\n",
        "lib/sub/__init__.py": "",
        "lib/sub/mod.py": "class Foo:\n    pass\n\n\ndef bar():\n    pass\n",
    })
    cfg = GitPythonProjectConfig(repo_uri=str(root), project_name="libproj")
    inc = falkordb_connection.select_graph(f"rx_inc_{uuid4().hex}")
    full = falkordb_connection.select_graph(f"rx_full_{uuid4().hex}")
    try:
        sync(GraphWriter(inc), cfg, "main", cache_root=tmp_path / "c_inc", engine="ast")
        (root / "lib/__init__.py").write_text(new_init)
        repo.git.add(A=True)
        repo.index.commit("edit __init__")
        sync(GraphWriter(inc), cfg, "main", cache_root=tmp_path / "c_inc", engine="ast")
        sync(GraphWriter(full), cfg, "main", cache_root=tmp_path / "c_full", engine="ast")
        assert _reexports(inc) == _reexports(full)
    finally:
        inc.delete()
        full.delete()


# -- an edge healed through an alias follows the alias when the re-export changes -----------------

@pytest.mark.parametrize("change,expect", [
    ({"lib/__init__.py": "from .sub.other import Foo\n"}, "lib.sub.other.class.foo"),   # retargeted
    ({"lib/__init__.py": "x = 1\n"}, None),                                                # removed
    ({"lib/__init__.py": "from .sub import Foo\n", "lib/sub/__init__.py": ""}, None),      # chain broken
])
def test_alias_healed_edge_follows_reexport_changes(falkordb_connection, tmp_path, change, expect):
    from uuid import uuid4

    from auryn.ingestors.python.git_sync import GitPythonProjectConfig, sync

    root = tmp_path / "lib"
    repo = _git_repo(root, {
        "lib/__init__.py": "from .sub import Foo\n",
        "lib/sub/__init__.py": "from .mod import Foo\n",
        "lib/sub/mod.py": "class Foo:\n    pass\n",
        "lib/sub/other.py": "class Foo:\n    pass\n",
    })
    app = _write(tmp_path / "approot", {"app/main.py": "from lib import Foo\ndef run():\n    Foo()\n"})
    g = falkordb_connection.select_graph(f"rx_edge_{uuid4().hex}")
    cfg = GitPythonProjectConfig(repo_uri=str(root), project_name="libproj")
    try:
        sync(GraphWriter(g), cfg, "main", cache_root=tmp_path / "c", engine="ast")
        _ingest(g, app, "appproj")
        assert _call_targets(g)["Foo"][0] == "python://libproj/module/lib.sub.mod.class.foo"

        for rel, text in change.items():
            (root / rel).write_text(text)
        repo.git.add(A=True)
        repo.index.commit("change re-exports")
        sync(GraphWriter(g), cfg, "main", cache_root=tmp_path / "c", engine="ast")

        targets = g.ro_query(
            "MATCH (:Function {name: 'run'})-[:CALLS]->(t) RETURN t.id"
        ).result_set
        ids = [t[0] for t in targets]
        assert "python://libproj/module/lib.sub.mod.class.foo" not in ids, "stale link survived"
        if expect:
            assert f"python://libproj/module/{expect}" in ids
    finally:
        g.delete()
