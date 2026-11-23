"""Integration tests for Python ingestion wiring, against the real `graph` fixture.

Proves the Python ingestor is registered as the owner of `.py` files in BOTH entry
points of the multi-language endpoint -- `ingest_directory` (local) and
`ingest_repository` (git) -- that the default `jedi` engine runs end-to-end (inferred
types + receiver-resolved calls), and that the `ast` engine is selectable as the shallower
zero-dep fallback. Uses the demo sample source as the corpus. Needs FalkorDB (provided by
the conftest fixture).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import git
import pytest

from auryn.core import ingest
from auryn.core.graph_client import GraphWriter
from auryn.ingestors.python.git_sync import GitPythonProjectConfig, sync

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SAMPLE = _REPO_ROOT / "demo" / "python-ingestor" / "sample"


@pytest.fixture
def sample_repo(tmp_path):
    origin = tmp_path / "sample_app"
    shutil.copytree(_SAMPLE, origin)
    repo = git.Repo.init(str(origin), initial_branch="main")
    with repo.config_writer() as cw:
        cw.set_value("user", "email", "t@e.com")
        cw.set_value("user", "name", "T")
    repo.git.add(A=True)
    commit = repo.index.commit("init").hexsha
    return origin, commit


def test_ingest_directory_owns_python(graph):
    report = ingest.ingest_directory(GraphWriter(graph), str(_SAMPLE), project_name="demo")

    ingested = {r.language: r for r in report.languages if r.result is not None}
    assert "python" in ingested
    assert ingested["python"].files_detected == 5  # app/__init__.py + models + db + widget + api
    # Now that python claims `.py`, the endpoint must NOT report it as unsupported.
    assert not any(u["language"] == "python" for u in report.unsupported_detected)
    assert graph.query(
        "MATCH (:Class {name:'Widget'})-[:EXTENDS]->(:Class {name:'Entity'}) RETURN 1"
    ).result_set == [[1]]


def test_ingest_repository_owns_python(graph, sample_repo):
    origin, commit = sample_repo
    report = ingest.ingest_repository(GraphWriter(graph), str(origin), ref=commit, project_name="demo")

    assert "python" in [r.language for r in report.languages if r.result is not None]
    assert graph.query(
        "MATCH (:Method {name:'render'})-[:CALLS]->(:Function {name:'get_connection'}) RETURN 1"
    ).result_set == [[1]]
    assert graph.query(
        "MATCH (:Function {name:'get_connection'})-[:RETURNS]->(:Class {name:'Connection'}) RETURN 1"
    ).result_set == [[1]]


def test_directory_resync_same_content_is_a_noop(graph):
    ingest.ingest_directory(GraphWriter(graph), str(_SAMPLE), project_name="demo")
    report = ingest.ingest_directory(GraphWriter(graph), str(_SAMPLE), project_name="demo")
    r = next(x.result for x in report.languages if x.language == "python")
    assert r is not None
    assert (r.nodes_added, r.nodes_removed, r.edges_added, r.edges_removed) == (0, 0, 0, 0)


@pytest.mark.parametrize("source_kind", ["directory", "worktree", "repository"])
def test_deep_pass_honors_source_filters(graph, tmp_path, source_kind, monkeypatch):
    monkeypatch.delenv("AURYN_INCLUDE_GENERATED", raising=False)
    root = tmp_path / "project"
    files = {
        "src/app.py": "def keep():\n    return 1\n",
        "src/excluded/manual.py": "def excluded():\n    return 2\n",
        "src/extension.skip.py": "def excluded_suffix():\n    return 2\n",
        "src/generated.py": "# @generated\ndef generated():\n    return 3\n",
        "src/vendor/legacy.py": "print 'vendored Python 2'\n",
        "deps/RediSearch/bin/linux-x64-release/search-community/_deps/boost-src/legacy.py":
            "print 'downloaded Python 2'\n",
        "outside.py": "def outside():\n    return 4\n",
        ".gitignore": "src/ignored.py\n",
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    if source_kind != "directory":
        repo = git.Repo.init(str(root), initial_branch="main")
        repo.git.add(A=True)
        actor = git.Actor("Test", "test@example.com")
        repo.index.commit("init", author=actor, committer=actor)
        (root / "src/ignored.py").write_text("print 'ignored Python 2'\n")

    ingest_source = ingest.ingest_repository if source_kind == "repository" else ingest.ingest_directory
    report = ingest_source(
        GraphWriter(graph), str(root), project_name="filtered",
        include_dirs=("src",), exclude_dirs=("excluded",),
        exclude_suffixes=(".skip.py",), deep="inline",
    )

    py = next(r for r in report.languages if r.language == "python")
    assert py.files_detected == 1
    assert py.deep_status == "inline"
    assert py.deep_result.files_parsed == 1
    assert py.deep_result.broken_files == []
    assert graph.query("MATCH (f:Function) RETURN f.name").result_set == [["keep"]]


def test_deep_pass_materializes_export_ignored_python_file(graph, tmp_path):
    root = tmp_path / "export_ignored"
    root.mkdir()
    (root / "kept.py").write_text("def kept():\n    return 1\n")
    (root / "ignored_from_archive.py").write_text("def included():\n    return 2\n")
    (root / ".gitattributes").write_text("ignored_from_archive.py export-ignore\n")
    repo = git.Repo.init(str(root), initial_branch="main")
    repo.git.add(A=True)
    actor = git.Actor("Test", "test@example.com")
    commit = repo.index.commit("init", author=actor, committer=actor).hexsha

    report = ingest.ingest_repository(
        GraphWriter(graph), str(root), ref=commit, project_name="export_ignored", deep="inline",
    )

    py = next(r for r in report.languages if r.language == "python")
    assert py.deep_result.files_parsed == 2
    assert graph.query("MATCH (f:Function) RETURN f.name ORDER BY f.name").result_set == [
        ["included"], ["kept"],
    ]


def test_deep_pass_can_include_generated_and_vendored_files(graph, tmp_path, monkeypatch):
    monkeypatch.setenv("AURYN_INCLUDE_GENERATED", "1")
    (tmp_path / "generated.py").write_text("# @generated\ndef generated():\n    return 1\n")
    deps = tmp_path / "_deps" / "library"
    deps.mkdir(parents=True)
    (deps / "helper.py").write_text("def helper():\n    return 2\n")

    report = ingest.ingest_directory(GraphWriter(graph), str(tmp_path), deep="inline")

    py = next(r for r in report.languages if r.language == "python")
    assert py.files_detected == py.deep_result.files_parsed == 2
    assert py.deep_result.broken_files == []
    assert graph.query("MATCH (f:Function) RETURN f.name ORDER BY f.name").result_set == [
        ["generated"], ["helper"],
    ]


def test_default_engine_is_jedi_and_resolves_receiver_calls(graph, sample_repo):
    # The default engine is "jedi": it must stamp the commit sha AND produce the inferred
    # receiver-typed call edge that the ast engine can't (conn.execute -> Connection.execute).
    origin, commit = sample_repo
    stats = sync(GraphWriter(graph), GitPythonProjectConfig(str(origin), "demo"), ref=commit)
    assert stats.nodes_added > 0
    assert graph.query("MATCH (p:Project {name:'demo'}) RETURN p.commit_sha").result_set == [[commit]]
    assert graph.query(
        "MATCH (:Method {name:'render'})-[c:CALLS {resolution:'inferred'}]->"
        "(:Method {name:'execute'}) RETURN c.receiver_type"
    ).result_set == [["Connection"]]


def test_ast_engine_is_selectable_and_shallower(graph, sample_repo):
    # engine="ast" is the zero-dep fallback: same backbone, but it can't reach the
    # receiver-typed call, so no inferred CALLS edge into Connection.execute.
    origin, commit = sample_repo
    sync(GraphWriter(graph), GitPythonProjectConfig(str(origin), "demo"), ref=commit, engine="ast")
    assert graph.query("MATCH (:Class {name:'Widget'})-[:EXTENDS]->(:Class {name:'Entity'}) RETURN 1").result_set == [[1]]
    assert graph.query(
        "MATCH (:Method {name:'render'})-[:CALLS]->(:Method {name:'execute'}) RETURN 1"
    ).result_set == []


# ── two-phase (fast ast -> deep jedi) orchestration by the endpoint ──────────────────────

def _depth(graph):
    r = graph.ro_query("MATCH (p:Project {name:'demo'}) RETURN p.depth").result_set
    return r[0][0] if r else None


def _has_inferred_receiver_call(graph):
    # The receiver-typed inferred CALLS edge (render -> Connection.execute) that only the
    # deep (jedi) pass can produce -- a proxy for "the graph is deep".
    return graph.query(
        "MATCH (:Method {name:'render'})-[:CALLS {resolution:'inferred'}]->(:Method {name:'execute'}) RETURN 1"
    ).result_set == [[1]]


def test_deep_off_leaves_a_shallow_fast_graph(graph):
    report = ingest.ingest_directory(GraphWriter(graph), str(_SAMPLE), project_name="demo", deep="off")

    py = next(r for r in report.languages if r.language == "python")
    assert py.deep_status == "skipped"
    assert _depth(graph) == "fast"
    assert not _has_inferred_receiver_call(graph)  # ast-only: no receiver-typed call


def test_deep_async_default_downgrades_to_inline_on_embedded_backend(graph):
    # The default is deep="async", but a detached daemon can't reach this in-process embedded DB, so
    # it downgrades to inline: the fast (ast) pass runs, then the deep (jedi) pass upgrades the same
    # nodes in place -- ending at the full jedi graph, synchronously, exactly as deep="inline" would.
    from auryn.core.ingest import _daemon_can_reach

    if _daemon_can_reach(GraphWriter(graph)):  # a server backend WOULD queue to the daemon; skip that path
        pytest.skip("server backend: async queues to the deepen daemon, not the embedded-downgrade path")
    report = ingest.ingest_directory(GraphWriter(graph), str(_SAMPLE), project_name="demo")

    py = next(r for r in report.languages if r.language == "python")
    assert py.deep_status == "inline"  # async -> inline (embedded backend unreachable by a daemon)
    assert py.deep_result is not None and py.deep_result.nodes_updated > 0  # upgraded, not re-added
    assert _depth(graph) == "deep"
    assert _has_inferred_receiver_call(graph)


def test_deep_background_returns_shallow_then_upgrades(graph):
    # background: the call returns as soon as the fast graph is written (depth "fast"), and a
    # daemon thread upgrades it in place to "deep" on a sibling writer.
    report = ingest.ingest_directory(GraphWriter(graph), str(_SAMPLE), project_name="demo", deep="background")

    py = next(r for r in report.languages if r.language == "python")
    assert py.deep_status == "background"
    assert _depth(graph) == "fast"  # deep upgrade hasn't landed yet
    assert not _has_inferred_receiver_call(graph)

    ingest.join_deep_phase(timeout=120)  # await the background upgrade

    assert _depth(graph) == "deep"
    assert _has_inferred_receiver_call(graph)


def test_invalid_deep_mode_is_rejected(graph):
    with pytest.raises(ValueError, match="deep must be one of"):
        ingest.ingest_directory(GraphWriter(graph), str(_SAMPLE), project_name="demo", deep="sometimes")


# -- Streaming resolution (recommendation B): build_plan + emit_streaming must be a
#    byte-for-byte equivalent of resolve() over the whole module list. --

def test_emit_streaming_matches_resolve():
    from auryn.core.graph_model import GraphEdge, GraphNode
    from auryn.ingestors.python import resolution
    from auryn.ingestors.python.file_parser import parse_module
    from auryn.ingestors.python.ids import package_dirs_from_paths

    srcs = {
        "pkg/__init__.py": b"",
        "pkg/base.py": b"class Base:\n    def greet(self):\n        return 1\n",
        "pkg/sub.py": b"from pkg.base import Base\n\nclass Sub(Base):\n"
                      b"    def run(self):\n        return self.greet()\n",
        "app.py": b"from pkg.sub import Sub\nimport os\n\n"
                  b"def main():\n    s = Sub()\n    return os.getpid() + s.run()\n",
    }
    paths = list(srcs)
    pkg_dirs = package_dirs_from_paths(paths)
    proj = "py://demo"

    def multisets(items):
        nodes: dict = {}
        edges: dict = {}
        for it in items:
            if isinstance(it, GraphNode):
                k = (it.label, it.key["id"], tuple(sorted((p, repr(v)) for p, v in it.properties.items())))
                nodes[k] = nodes.get(k, 0) + 1
            else:
                k = (it.rel_type, it.from_key["id"], it.to_key["id"],
                     tuple(sorted((p, repr(v)) for p, v in it.properties.items())))
                edges[k] = edges.get(k, 0) + 1
        return nodes, edges

    a_nodes, a_edges = multisets(resolution.resolve([parse_module(srcs[p], p, pkg_dirs) for p in paths], proj))

    refs = [(p, p) for p in paths]  # blob_sha stand-in = path for the test

    def load_fn(path, _sha):  # re-parse each call (as git_sync does from the blob)
        return parse_module(srcs[path], path, pkg_dirs)

    plan = resolution.build_plan(refs, load_fn, proj)
    b_nodes, b_edges = multisets(list(resolution.emit_streaming(plan, load_fn, proj)))

    assert b_nodes == a_nodes
    assert b_edges == a_edges
    # sanity: the corpus exercised the cross-module cases, not an empty match
    assert any(lbl == "Package" for (lbl, _nid, _p) in a_nodes)                     # pkg/__init__.py
    assert any(rel == "CALLS" for (rel, _f, _t, _p) in a_edges)                     # self.greet()/s.run()
    assert any(rel in ("INHERITS_FROM", "EXTENDS") for (rel, _f, _t, _p) in a_edges)  # Sub(Base) cross-module
    assert plan.broken == []


def test_incremental_seeds_class_init_for_constructor_resolution(graph, tmp_path):
    # #785 review: a constructor call collects only the CLASS name (not "__init__"), so the incremental
    # symbol seed must still load the class's __init__ -- else the no-local-__init__ class-node fallback
    # wrongly fires on a resync for a class that DOES define one (full vs incremental divergence).
    from auryn.ingestors.python import incremental

    (tmp_path / "svc.py").write_text("class Service:\n    def __init__(self):\n        pass\n")
    (tmp_path / "app.py").write_text("from svc import Service\n\ndef boot():\n    return Service()\n")
    ingest.ingest_directory(GraphWriter(graph), str(tmp_path), project_name="demo")
    # a resync of only app.py references the class name 'Service', never '__init__'
    idx = incremental.read_graph_index(
        GraphWriter(graph), "python://demo", exclude_keys={"app"}, names={"Service", "boot"}
    )
    assert "svc.Service.__init__" in idx.callable_by_qname
