"""Delta-sync (commit-switching) tests for the Kotlin ingestor, against a real 2-commit local
git repo (GitPython in tmp_path, no network) and the real `graph` fixture (embedded FalkorDB).

Drives `git_sync.sync_at` directly over a `GitRepoCache` -- the same recompute-and-diff mechanism
(core/graph_sync.stream_apply) the other language ingestors share -- and proves the three
properties that make commit-switching safe:

  1. a forward switch (c1 -> c2) applies exactly the delta between the two commits;
  2. returning to a commit reproduces that commit's graph *byte-for-byte* (the round-trip
     invariant -- the whole point of "recompute the full target state and diff", rather than
     hand-deriving edits from a source-level diff);
  3. re-syncing the same commit is a zero-delta no-op.

The two commits deliberately exercise every kind of delta at once: a removed member (`b`), an
added member (`c`), a *modified* member whose body now calls something else (`a`: b() -> c()),
and an added sibling type (`Bar`). All calls are intra-type, so no ADR-0004 cross-repo
placeholders (which are `extern://`-keyed, outside this project's id prefix) enter the picture --
keeping the round-trip a clean same-project comparison.
"""

from __future__ import annotations

from pathlib import Path

import git
import pytest

from auryn.core.git_source import GitRepoCache
from auryn.core.graph_client import GraphWriter
from auryn.ingestors.kotlin.git_sync import sync_at

_PROJECT = "demo"
_REPO_URI = "test://demo"  # stored verbatim on the Project node; constant so the round-trip matches

# commit 1: class Foo with fun a() (calls b) and fun b(); plus an import.
_FOO_V1 = """\
package p

import kotlin.math.max

class Foo {
    fun a() {
        b()
    }

    fun b() {
    }
}
"""

# commit 2: b removed, c added, a changed to call c instead of b, and a new sibling class Bar.
_FOO_V2 = """\
package p

import kotlin.math.max

class Foo {
    fun a() {
        c()
    }

    fun c() {
    }
}

class Bar
"""


@pytest.fixture
def origin_repo(tmp_path):
    """A local git repo with two commits of Kotlin, returned as (path, c1_sha, c2_sha)."""
    origin = tmp_path / "origin"
    repo = git.Repo.init(str(origin), initial_branch="main")
    with repo.config_writer() as cw:
        cw.set_value("user", "email", "t@e.com")
        cw.set_value("user", "name", "T")

    (origin / "app").mkdir()
    (origin / "app" / "Foo.kt").write_text(_FOO_V1)
    repo.index.add(["app/Foo.kt"])
    c1 = repo.index.commit("Foo with a() and b()").hexsha

    (origin / "app" / "Foo.kt").write_text(_FOO_V2)
    repo.index.add(["app/Foo.kt"])
    c2 = repo.index.commit("remove b, add c and Bar; a now calls c").hexsha

    return origin, c1, c2


@pytest.fixture
def source(origin_repo, tmp_path):
    """A GitRepoCache (a bare clone of the origin) -- the duck-typed `source` sync_at reads."""
    origin, _, _ = origin_repo
    cache = GitRepoCache(str(origin), tmp_path / "cache")
    cache.fetch()
    return cache


def _sync(graph, source, sha):
    return sync_at(
        GraphWriter(graph), source, sha, project_name=_PROJECT, repo_uri=_REPO_URI, ref=sha
    )


def q(graph, cypher):
    return graph.query(cypher).result_set


# ── snapshot helper: a canonical, order-independent view of the ENTIRE graph ──────────────
#
# Nodes keyed by their application-level `id` property (not FalkorDB's volatile internal id, which
# a delete+re-add would reassign); edges keyed by (from-id, type, to-id). Both carry their sorted
# non-volatile properties -- `_`-prefixed bookkeeping (GraphWriter's `_fingerprint`) and any
# worker-written `embedding` vector are excluded, as they aren't part of the ingestor's semantic
# output. Two snapshots are `==` iff the graphs are structurally identical.

_VOLATILE_KEYS = {"embedding"}


def _canon(value):
    # list-valued props (e.g. Enum members) -> tuples, so the snapshot elements stay hashable.
    if isinstance(value, list):
        return tuple(_canon(v) for v in value)
    return value


def _props(properties: dict):
    return tuple(
        sorted(
            (k, _canon(v))
            for k, v in properties.items()
            if not k.startswith("_") and k not in _VOLATILE_KEYS
        )
    )


def _snapshot(graph):
    nodes = frozenset(
        (node_id, tuple(sorted(labels)), _props(properties))
        for node_id, labels, properties in q(graph, "MATCH (n) RETURN n.id, labels(n), properties(n)")
    )
    edges = frozenset(
        (from_id, rel_type, to_id, _props(properties))
        for from_id, rel_type, to_id, properties in q(
            graph, "MATCH (a)-[r]->(b) RETURN a.id, type(r), b.id, properties(r)"
        )
    )
    return nodes, edges


# ── tests ─────────────────────────────────────────────────────────────────────────────────


def test_forward_switch_applies_correct_delta(graph, source, origin_repo):
    _, c1, c2 = origin_repo

    _sync(graph, source, c1)
    # c1 state: Foo.a and Foo.b both present, a calls b, no c, no Bar.
    # app/Foo.kt is a root-level file (no src/main/kotlin subproject), so its package hangs off the
    # Project directly and there is NO Module tier (that appears only with real subprojects).
    assert q(graph, "MATCH (:Project)-[:HAS_PACKAGE]->(:Package {name:'p'}) RETURN count(*)") == [[1]]
    assert q(graph, "MATCH (:Module) RETURN count(*)") == [[0]]
    assert q(graph, "MATCH (m:Method {name:'a'}) RETURN count(m)") == [[1]]
    assert q(graph, "MATCH (m:Method {name:'b'}) RETURN count(m)") == [[1]]
    assert q(graph, "MATCH (:Method {name:'a'})-[:CALLS]->(:Method {name:'b'}) RETURN count(*)") == [[1]]
    assert q(graph, "MATCH (m:Method {name:'c'}) RETURN count(m)") == [[0]]
    assert q(graph, "MATCH (c:Class {name:'Bar'}) RETURN count(c)") == [[0]]

    stats = _sync(graph, source, c2)

    # The delta actually did work in every direction: removed b, added c + Bar, updated a.
    assert stats.commit_sha == c2
    assert stats.nodes_removed >= 1  # at least Foo.b
    assert stats.nodes_added >= 1    # at least Foo.c and class Bar
    assert stats.nodes_updated >= 1  # Foo.a's body changed (b() -> c())

    # c2 state: b gone, c present, Bar present, a now calls c (and no longer calls b).
    assert q(graph, "MATCH (m:Method {name:'b'}) RETURN count(m)") == [[0]]
    assert q(graph, "MATCH (m:Method {name:'c'}) RETURN count(m)") == [[1]]
    assert q(graph, "MATCH (m:Method {name:'a'}) RETURN count(m)") == [[1]]
    assert q(graph, "MATCH (c:Class {name:'Bar'}) RETURN count(c)") == [[1]]
    assert q(graph, "MATCH (:Method {name:'a'})-[:CALLS]->(:Method {name:'c'}) RETURN count(*)") == [[1]]
    assert q(graph, "MATCH (:Method {name:'a'})-[:CALLS]->(:Method {name:'b'}) RETURN count(*)") == [[0]]
    # the Project node advanced to the new commit.
    assert q(graph, "MATCH (p:Project {id:'kotlin://demo'}) RETURN p.commit_sha") == [[c2]]


def test_commit_roundtrip_restores_identical_graph(graph, source, origin_repo):
    """KEY invariant: sync c1, then c1 -> c2 -> c1, and the graph must be byte-for-byte what it
    was after the first c1 ingest -- recompute-and-diff makes returning to a commit reproduce that
    commit's exact state, not merely something close to it."""
    _, c1, c2 = origin_repo

    _sync(graph, source, c1)
    at_c1 = _snapshot(graph)
    assert at_c1[0] and at_c1[1]  # sanity: the first ingest produced a non-empty graph

    _sync(graph, source, c2)
    at_c2 = _snapshot(graph)
    assert at_c2 != at_c1  # sanity: c2 really is a different graph, so the round-trip is non-trivial

    _sync(graph, source, c1)
    back_at_c1 = _snapshot(graph)

    assert back_at_c1 == at_c1


def test_resync_same_commit_is_zero_delta_noop(graph, source, origin_repo):
    _, c1, _ = origin_repo
    _sync(graph, source, c1)

    stats = _sync(graph, source, c1)

    assert stats.commit_sha == c1
    assert (
        stats.nodes_added,
        stats.nodes_updated,
        stats.nodes_removed,
        stats.edges_added,
        stats.edges_updated,
        stats.edges_removed,
    ) == (0, 0, 0, 0, 0, 0)
