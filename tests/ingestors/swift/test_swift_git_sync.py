"""Delta-sync (commit-switching) integration tests for the Swift ingestor, against the real
`graph` fixture and a local two-commit git repo built with GitPython in tmp_path (no network).

The Swift ingestor's git entry point is `git_sync.sync_at(writer, source, target_sha, ...)`,
which fully recomputes the Swift subgraph at `target_sha` and diffs it onto the graph (the same
recompute-and-diff model every other language uses). These tests drive it exactly as the
multi-language endpoint does -- over a GitRepoCache bare clone -- and prove:

* a forward commit switch applies the correct delta (a removed method vanishes, a new method and
  a new type appear, a modified method's call target follows the source),
* the switch is *reversible*: returning to the original commit reproduces the original graph
  byte-for-byte (the property that makes moving between any two commits safe), and
* re-syncing the commit the graph is already at is a zero-delta no-op.

NOTE (Swift gotcha flagged by the ingestor builder): single-line concatenated Swift makes
tree-sitter emit ERROR nodes, so every fixture below is formatted with real newlines between
declarations.
"""

from __future__ import annotations

import git
import pytest

from auryn.core.git_source import GitRepoCache
from auryn.core.graph_client import GraphWriter
from auryn.ingestors.swift.git_sync import sync_at
from auryn.ingestors.swift.ids import project_id

_PROJECT = "demo"
_PROJ_ID = project_id(_PROJECT)

# commit 1: struct Foo with a() and b(), where a calls b (implicit-self bare call), plus an import.
_FOO_V1 = """\
import Foundation

struct Foo {
    func a() {
        b()
    }
    func b() {
    }
}
"""

# commit 2: b removed, c added, a rewritten to call c instead of b, and a new top-level struct Bar.
_FOO_V2 = """\
import Foundation

struct Foo {
    func a() {
        c()
    }
    func c() {
    }
}

struct Bar {
}
"""


@pytest.fixture
def origin_repo(tmp_path):
    """A real local git repo with two commits that modify a single Swift file."""
    origin_path = tmp_path / "origin"
    repo = git.Repo.init(str(origin_path), initial_branch="main")
    with repo.config_writer() as cw:
        cw.set_value("user", "email", "test@example.com")
        cw.set_value("user", "name", "Test")

    (origin_path / "Sources").mkdir()
    (origin_path / "Sources" / "Foo.swift").write_text(_FOO_V1)
    repo.index.add(["Sources/Foo.swift"])
    first_commit = repo.index.commit("Foo with a() and b()").hexsha

    (origin_path / "Sources" / "Foo.swift").write_text(_FOO_V2)
    repo.index.add(["Sources/Foo.swift"])
    second_commit = repo.index.commit("drop b, add c and Bar, a now calls c").hexsha

    return origin_path, first_commit, second_commit


@pytest.fixture
def source(origin_repo, tmp_path):
    origin_path, _, _ = origin_repo
    cache = GitRepoCache(str(origin_path), tmp_path / "cache")
    cache.fetch()
    return cache


def _sync(writer, source, origin_path, sha):
    return sync_at(
        writer, source, sha, project_name=_PROJECT, repo_uri=str(origin_path), ref="main"
    )


_VOLATILE_PROPS = frozenset({"_fingerprint", "embedding"})


def _snapshot(graph):
    """A canonical, order-independent snapshot of the WHOLE graph, safe to compare with `==`:

    * every node as (sorted labels, id, sorted non-volatile props), and
    * every edge as (from-id, rel-type, to-id, sorted props).

    Internal bookkeeping (`_fingerprint`) and worker-managed embedding state are excluded so the
    snapshot reflects the ingestor's own semantic output; list-valued props are frozen to tuples
    so the result is hashable into a set. Returning to a commit must reproduce this exactly.
    """

    def freeze(v):
        return tuple(freeze(x) for x in v) if isinstance(v, list) else v

    def props(mapping):
        return tuple(sorted((k, freeze(v)) for k, v in mapping.items() if k not in _VOLATILE_PROPS))

    nodes = frozenset(
        (
            tuple(sorted(lbl for lbl in n.labels if lbl != GraphWriter.SHARED_EMBEDDING_LABEL)),
            n.properties.get("id"),
            props(n.properties),
        )
        for (n,) in graph.query("MATCH (n) RETURN n").result_set
    )
    edges = frozenset(
        (from_id, r.relation, to_id, props(r.properties))
        for from_id, r, to_id in graph.query("MATCH (a)-[r]->(b) RETURN a.id, r, b.id").result_set
    )
    return nodes, edges


def _q(graph, cypher):
    return graph.query(cypher).result_set


def test_forward_switch_applies_correct_delta(origin_repo, graph, source):
    origin_path, first_commit, second_commit = origin_repo
    writer = GraphWriter(graph)

    _sync(writer, source, origin_path, first_commit)
    # Baseline at c1: Foo with a() and b() present; a calls b; no c, no Bar.
    assert _q(graph, "MATCH (m:Method {name: 'a'}) RETURN m.id") != []
    assert _q(graph, "MATCH (m:Method {name: 'b'}) RETURN m.id") != []
    assert _q(graph, "MATCH (:Method {name: 'a'})-[:CALLS]->(:Method {name: 'b'}) RETURN 1") == [[1]]
    assert _q(graph, "MATCH (m:Method {name: 'c'}) RETURN m.id") == []
    assert _q(graph, "MATCH (s:Struct {name: 'Bar'}) RETURN s.id") == []

    stats = _sync(writer, source, origin_path, second_commit)

    assert stats.commit_sha == second_commit
    assert _q(graph, f"MATCH (p:Project {{id: '{_PROJ_ID}'}}) RETURN p.commit_sha") == [[second_commit]]
    # b is gone, c and Bar have appeared.
    assert _q(graph, "MATCH (m:Method {name: 'b'}) RETURN m.id") == []
    assert _q(graph, "MATCH (m:Method {name: 'c'}) RETURN m.id") != []
    assert _q(graph, "MATCH (s:Struct {name: 'Bar'}) RETURN s.id") != []
    # a's call target followed the source edit: a -> c now, and no longer a -> b.
    assert _q(graph, "MATCH (:Method {name: 'a'})-[:CALLS]->(:Method {name: 'c'}) RETURN 1") == [[1]]
    assert _q(graph, "MATCH (:Method {name: 'a'})-[:CALLS]->(:Method {name: 'b'}) RETURN count(*)") == [[0]]
    # a and the Foo struct persisted across the switch.
    assert _q(graph, "MATCH (:Struct {name: 'Foo'})-[:HAS_METHOD]->(m:Method {name: 'a'}) RETURN 1") == [[1]]


def test_commit_roundtrip_restores_identical_graph(origin_repo, graph, source):
    origin_path, first_commit, second_commit = origin_repo
    writer = GraphWriter(graph)

    _sync(writer, source, origin_path, first_commit)
    before = _snapshot(graph)
    assert before[0] and before[1]  # sanity: the c1 graph is non-empty (has nodes and edges)

    _sync(writer, source, origin_path, second_commit)  # forward
    _sync(writer, source, origin_path, first_commit)   # back to the original commit
    after = _snapshot(graph)

    assert after == before


def test_resync_same_commit_is_zero_delta_noop(origin_repo, graph, source):
    origin_path, first_commit, _ = origin_repo
    writer = GraphWriter(graph)

    _sync(writer, source, origin_path, first_commit)
    stats = _sync(writer, source, origin_path, first_commit)

    assert stats.commit_sha == first_commit
    assert (stats.nodes_added, stats.nodes_updated, stats.nodes_removed) == (0, 0, 0)
    assert (stats.edges_added, stats.edges_updated, stats.edges_removed) == (0, 0, 0)
