"""Delta-sync (commit-switching) tests for the Bash ingestor, against the real `graph` fixture
and a local two-commit git repo built with GitPython in tmp_path (no network needed).

Bash's git entry point is `git_sync.sync_at(writer, source, target_sha, ...)` where `source` is a
GitRepoCache (or LocalDirectorySource) -- it fully recomputes the Bash subgraph at `target_sha`
and diffs it onto the graph (the same recompute-and-delta model as the other tree-sitter
ingestors). Because a bare clone has no working-tree state, moving between any two commits is the
same operation regardless of direction, so the round-trip below is a real invariant, not luck.

Bash's model is flat: File -[HAS_FUNCTION]-> Function -[CALLS]-> Function and File -[IMPORTS]->
File (`source`/`.`). The corpus exercises node add/remove (bar removed, baz added), edge
retargeting (foo's CALLS moves bar -> baz), and edge removal (the `source` IMPORTS edge is
dropped) across the two commits.
"""

from __future__ import annotations

import git
import pytest

from auryn.core.git_source import GitRepoCache
from auryn.core.graph_client import GraphWriter
from auryn.ingestors.bash.git_sync import sync_at

# lib.sh is present in both commits and never edited -- it only exists so commit 1's
# `source ./lib.sh` resolves to a project File (an IMPORTS edge), which commit 2 then drops.
_LIB_SH = "helper() {\n    echo hi\n}\n"

# commit 1: foo calls bar; both defined in app.sh; app.sh sources lib.sh.
_APP_SH_V1 = (
    "source ./lib.sh\n"
    "\n"
    "foo() {\n"
    "    bar\n"
    "}\n"
    "\n"
    "bar() {\n"
    "    echo b\n"
    "}\n"
)

# commit 2: bar removed, baz added, foo now calls baz, and the `source` line is gone.
_APP_SH_V2 = (
    "foo() {\n"
    "    baz\n"
    "}\n"
    "\n"
    "baz() {\n"
    "    echo z\n"
    "}\n"
)

_PROJECT = "demo"
_PROJ_ID = "bash://demo"


@pytest.fixture
def origin_repo(tmp_path):
    """A real local git repo with two commits of Bash (see module docstring)."""
    origin_path = tmp_path / "origin"
    repo = git.Repo.init(str(origin_path), initial_branch="main")
    with repo.config_writer() as cw:
        cw.set_value("user", "email", "test@example.com")
        cw.set_value("user", "name", "Test")

    (origin_path / "lib.sh").write_text(_LIB_SH)
    (origin_path / "app.sh").write_text(_APP_SH_V1)
    repo.index.add(["lib.sh", "app.sh"])
    first_commit = repo.index.commit("foo calls bar; source lib.sh").hexsha

    (origin_path / "app.sh").write_text(_APP_SH_V2)
    repo.index.add(["app.sh"])
    second_commit = repo.index.commit("remove bar, add baz; foo calls baz; drop source").hexsha

    return origin_path, first_commit, second_commit


@pytest.fixture
def cache(origin_repo, tmp_path):
    origin_path, _, _ = origin_repo
    c = GitRepoCache(str(origin_path), tmp_path / "cache")
    c.fetch()  # one bare clone holds both commits; sync_at reads blobs at either sha
    return c


def _sync(writer, cache, repo_uri, sha):
    return sync_at(writer, cache, sha, project_name=_PROJECT, repo_uri=repo_uri, ref=sha)


def _snapshot(graph):
    """A canonical, order-independent snapshot of the whole graph: a set of ALL nodes
    (label, id, sorted non-volatile props) and a set of ALL edges (type, from-id, to-id,
    sorted props). Internal `_`-prefixed properties (e.g. the sync engine's `_fingerprint`)
    are excluded; every remaining Bash property is content-derived, so returning to a commit
    must reproduce this snapshot byte-for-byte."""

    def clean(props):
        return tuple(sorted((k, repr(v)) for k, v in props.items() if not k.startswith("_")))

    nodes = {
        (label, nid, clean(props))
        for label, nid, props in graph.query(
            "MATCH (n) RETURN labels(n)[0], n.id, properties(n)"
        ).result_set
    }
    edges = {
        (rel, aid, bid, clean(props))
        for rel, aid, bid, props in graph.query(
            "MATCH (a)-[r]->(b) RETURN type(r), a.id, b.id, properties(r)"
        ).result_set
    }
    return nodes, edges


def test_forward_switch_applies_correct_delta(origin_repo, cache, graph):
    origin_path, c1, c2 = origin_repo
    writer = GraphWriter(graph)

    _sync(writer, cache, str(origin_path), c1)

    def q(cypher):
        return graph.query(cypher).result_set

    # -- commit 1 state: foo + bar defined, foo CALLS bar, app.sh IMPORTS lib.sh, no baz --
    assert q("MATCH (:Function {name:'foo'}) RETURN 1") == [[1]]
    assert q("MATCH (:Function {name:'bar'}) RETURN 1") == [[1]]
    assert q("MATCH (:Function {name:'baz'}) RETURN 1") == []
    assert q("MATCH (:Function {name:'foo'})-[:CALLS]->(:Function {name:'bar'}) RETURN 1") == [[1]]
    assert q("MATCH (:File {name:'app.sh'})-[:IMPORTS]->(:File {name:'lib.sh'}) RETURN 1") == [[1]]
    assert q("MATCH (p:Project {id:'" + _PROJ_ID + "'}) RETURN p.commit_sha") == [[c1]]

    stats = _sync(writer, cache, str(origin_path), c2)
    assert stats.commit_sha == c2
    # Genuinely incremental over the bare-clone git diff (ADR 0002), not a full recompute: only the
    # one changed file (app.sh) is re-parsed; lib.sh is untouched and stays cached.
    assert stats.files_parsed == 1, f"expected a 1-file delta, re-parsed {stats.files_parsed}"
    assert stats.files_cached == 1

    # -- commit 2 delta: bar removed, baz added, foo's CALLS retargeted, IMPORTS dropped --
    assert q("MATCH (:Function {name:'bar'}) RETURN 1") == []            # bar removed
    assert q("MATCH (:Function {name:'baz'}) RETURN 1") == [[1]]          # baz added
    assert q("MATCH (:Function {name:'foo'}) RETURN 1") == [[1]]          # foo survives
    assert q("MATCH (:Function {name:'foo'})-[:CALLS]->(:Function {name:'baz'}) RETURN 1") == [[1]]
    assert q("MATCH (:Function {name:'foo'})-[:CALLS]->(:Function {name:'bar'}) RETURN 1") == []
    assert q("MATCH (:File)-[r:IMPORTS]->(:File) RETURN count(r)") == [[0]]  # source line gone
    assert q("MATCH (p:Project {id:'" + _PROJ_ID + "'}) RETURN p.commit_sha") == [[c2]]


def test_commit_roundtrip_restores_identical_graph(origin_repo, cache, graph):
    """KEY invariant: c1 -> c2 -> c1 must reproduce the exact graph that a fresh sync at c1
    produced -- no stale bar node/edge, no leftover baz, the IMPORTS edge back, commit_sha
    restored. A recompute-and-delta bug (residue on backward switch) would fail here."""
    origin_path, c1, c2 = origin_repo
    writer = GraphWriter(graph)

    _sync(writer, cache, str(origin_path), c1)
    before = _snapshot(graph)

    _sync(writer, cache, str(origin_path), c2)
    _sync(writer, cache, str(origin_path), c1)
    after = _snapshot(graph)

    before_nodes, before_edges = before
    after_nodes, after_edges = after
    assert after_nodes == before_nodes, (
        f"nodes differ after round-trip: "
        f"only-before={before_nodes - after_nodes}, only-after={after_nodes - before_nodes}"
    )
    assert after_edges == before_edges, (
        f"edges differ after round-trip: "
        f"only-before={before_edges - after_edges}, only-after={after_edges - before_edges}"
    )


def test_resync_same_commit_is_zero_delta_noop(origin_repo, cache, graph):
    origin_path, c1, _ = origin_repo
    writer = GraphWriter(graph)
    _sync(writer, cache, str(origin_path), c1)

    stats = _sync(writer, cache, str(origin_path), c1)

    assert (
        stats.nodes_added,
        stats.nodes_removed,
        stats.nodes_updated,
        stats.edges_added,
        stats.edges_removed,
        stats.edges_updated,
    ) == (0, 0, 0, 0, 0, 0)
    assert stats.commit_sha == c1
