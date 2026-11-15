"""Delta-sync (commit-switching) tests for the Ruby ingestor, against the real `graph`
fixture (embedded FalkorDB, no Docker) and a local two-commit git repo built with GitPython.

These exercise the recompute-and-diff model directly through `git_sync.sync_at(writer, source,
target_sha, ...)` with a `GitRepoCache` source (a bare clone of the origin -- moving between any
two commits is the same read, see core/git_source.py). They prove that a forward commit move
applies the correct add/remove/update deltas, that returning to a commit reproduces the original
graph exactly (nothing stale left behind, nothing missing), and that resyncing the same commit is
a zero-delta no-op -- the Ruby counterpart to every other language's test_<lang>_git_sync.py.
"""

from __future__ import annotations

import git
import pytest

from auryn.core.git_source import GitRepoCache
from auryn.core.graph_client import GraphWriter
from auryn.ingestors.ruby.git_sync import sync_at

# commit 1: class Foo with methods a, b (a calls b) + a top-level require.
_FOO_V1 = """\
require "set"

class Foo
  def a
    b
  end

  def b
  end
end
"""

# commit 2: drop b, add c, change a's body (now calls c), add a second class Bar.
_FOO_V2 = """\
require "set"

class Foo
  def a
    c
  end

  def c
  end
end

class Bar
end
"""

_PROJECT = "demo"
_REPO_URI_REF = "main"


@pytest.fixture
def ruby_repo(tmp_path):
    """A local repo with two commits of one Ruby file. Returns (origin_path, c1_sha, c2_sha)."""
    origin = tmp_path / "origin"
    repo = git.Repo.init(str(origin), initial_branch="main")
    with repo.config_writer() as cw:
        cw.set_value("user", "email", "t@e.com")
        cw.set_value("user", "name", "T")

    (origin / "lib").mkdir()
    (origin / "lib" / "foo.rb").write_text(_FOO_V1)
    repo.index.add(["lib/foo.rb"])
    c1 = repo.index.commit("v1: Foo with a, b").hexsha

    (origin / "lib" / "foo.rb").write_text(_FOO_V2)
    repo.index.add(["lib/foo.rb"])
    c2 = repo.index.commit("v2: drop b, add c, a->c, add Bar").hexsha

    return origin, c1, c2


def _source(origin, tmp_path):
    """A GitRepoCache over the local origin (bare clone into a temp cache), ready to read any sha."""
    cache = GitRepoCache(str(origin), tmp_path / "cache")
    cache.fetch()
    return cache


def _sync(writer, source, origin, sha):
    return sync_at(writer, source, sha, project_name=_PROJECT, repo_uri=str(origin), ref=_REPO_URI_REF)


# -- read helpers (literal cypher; names are test-controlled constants) ---------------------

def _method_names(graph, class_name: str) -> set[str]:
    rows = graph.query(
        f"MATCH (:Class {{name:'{class_name}'}})-[:HAS_METHOD]->(m:Method) RETURN m.name"
    ).result_set
    return {r[0] for r in rows}


def _class_exists(graph, name: str) -> bool:
    return graph.query(f"MATCH (c:Class {{name:'{name}'}}) RETURN 1").result_set != []


def _calls(graph, caller: str, callee: str) -> bool:
    return graph.query(
        f"MATCH (:Method {{name:'{caller}'}})-[:CALLS]->(:Method {{name:'{callee}'}}) RETURN 1"
    ).result_set != []


def _method_source(graph, name: str) -> str:
    return graph.query(f"MATCH (m:Method {{name:'{name}'}}) RETURN m.source").result_set[0][0]


# -- snapshot: a canonical, order-independent representation of the WHOLE graph -------------

def _canon_props(props: dict) -> tuple:
    # Drop the write-time `_fingerprint` (a hash of the real props -- not part of the logical
    # graph). Every remaining value the Ruby ingestor emits is a plain scalar (str/int/bool),
    # so a sorted tuple of (key, value) pairs is hashable and fully determines the props.
    return tuple(sorted((k, v) for k, v in props.items() if k != "_fingerprint"))


def _snapshot(graph):
    """Exact, order-independent snapshot of ALL nodes and ALL edges: a pair of frozensets so
    two snapshots compare equal iff the graphs are identical node-for-node and edge-for-edge."""
    nodes = frozenset(
        (label, nid, _canon_props(props))
        for label, nid, props in graph.ro_query(
            "MATCH (n) RETURN labels(n)[0], n.id, properties(n)"
        ).result_set
    )
    edges = frozenset(
        (rel, aid, bid, _canon_props(props))
        for rel, aid, bid, props in graph.ro_query(
            "MATCH (a)-[r]->(b) RETURN type(r), a.id, b.id, properties(r)"
        ).result_set
    )
    return nodes, edges


# -- tests ---------------------------------------------------------------------------------

def test_forward_switch_applies_correct_delta(ruby_repo, graph, tmp_path):
    """A forward commit move applies every delta kind: b removed, c added, a updated in place,
    Bar added."""
    origin, c1, c2 = ruby_repo
    source = _source(origin, tmp_path)
    writer = GraphWriter(graph)

    _sync(writer, source, origin, c1)
    # c1: Foo has a and b, and a calls b.
    assert _method_names(graph, "Foo") == {"a", "b"}
    assert _calls(graph, "a", "b")
    assert not _class_exists(graph, "Bar")
    a_source_c1 = _method_source(graph, "a")

    stats = _sync(writer, source, origin, c2)
    assert stats.commit_sha == c2
    # c2: b REMOVED, c ADDED, Bar ADDED, and a UPDATED in place (same node id, new body/call).
    assert _method_names(graph, "Foo") == {"a", "c"}   # b gone, c present
    assert _class_exists(graph, "Bar")
    assert not _calls(graph, "a", "b")                 # the old call was removed
    assert _calls(graph, "a", "c")                     # a now calls c
    assert _method_source(graph, "a") != a_source_c1   # a's node was updated, not recreated stale
    # The delta tally reflects the same story (a is an update, not an add/remove churn).
    assert stats.nodes_added >= 1      # Method c + Class Bar
    assert stats.nodes_removed >= 1    # Method b
    assert stats.nodes_updated >= 1    # Method a (body changed)


def test_commit_roundtrip_restores_identical_graph(ruby_repo, graph, tmp_path):
    """THE invariant: c1 -> c2 -> c1 reproduces the c1 graph byte-for-byte -- no stale nodes/edges
    left behind, nothing missing. Snapshots the full graph at c1, round-trips, and asserts EQUAL."""
    origin, c1, c2 = ruby_repo
    source = _source(origin, tmp_path)
    writer = GraphWriter(graph)

    _sync(writer, source, origin, c1)
    before = _snapshot(graph)

    _sync(writer, source, origin, c2)
    _sync(writer, source, origin, c1)
    after = _snapshot(graph)

    assert after == before


def test_resync_same_commit_is_zero_delta_noop(ruby_repo, graph, tmp_path):
    """Resyncing the commit the graph already holds changes nothing: every add/remove/update
    tally is zero."""
    origin, c1, _c2 = ruby_repo
    source = _source(origin, tmp_path)
    writer = GraphWriter(graph)

    _sync(writer, source, origin, c1)
    stats = _sync(writer, source, origin, c1)

    assert stats.commit_sha == c1
    assert (stats.nodes_added, stats.nodes_removed, stats.nodes_updated) == (0, 0, 0)
    assert (stats.edges_added, stats.edges_removed, stats.edges_updated) == (0, 0, 0)
