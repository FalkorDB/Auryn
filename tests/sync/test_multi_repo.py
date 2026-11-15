"""Multiple repos ingested into one graph: each is its own project subgraph
(python://<name>), and a resync of one must (a) be correct against a full recompute of that
project and (b) never touch another project's subgraph — the id-prefix scoping the
incremental reads rely on.
"""

from __future__ import annotations

import pytest

from ._helpers import (
    GitRepo, assert_graphs_equal, drop, dump, fresh_graph, ingest_repo, python_report,
)
from auryn.core.graph_client import GraphWriter

REPO_A = {
    "a_pkg/__init__.py": "",
    "a_pkg/core.py": "def compute(x):\n    return x * 2\n",
    "a_main.py": "from a_pkg.core import compute\n\n\ndef go():\n    return compute(5)\n",
}
REPO_B = {
    "b_pkg/__init__.py": "",
    "b_pkg/core.py": "def process(y):\n    return y - 1\n",
    "b_main.py": "from b_pkg.core import process\n\n\ndef run():\n    return process(9)\n",
}

# Non-colliding project names (one must not be a string prefix of the other — see the
# graph_client prefix caveat).
A, B = "alpha", "bravo"


@pytest.fixture
def repos(tmp_path):
    a = GitRepo.init(tmp_path / "a").write(REPO_A)
    b = GitRepo.init(tmp_path / "b").write(REPO_B)
    return a, b


def test_two_repos_one_graph_isolation_and_correctness(repos, falkordb_connection, tmp_path):
    a, b = repos
    cache = tmp_path / "cache"
    g = fresh_graph(falkordb_connection, "multi")
    fa = fresh_graph(falkordb_connection, "multi_a_full")
    try:
        a1 = a.commit("a1")
        b1 = b.commit("b1")
        ingest_repo(GraphWriter(g), a, a1, cache, project=A)
        ingest_repo(GraphWriter(g), b, b1, cache, project=B)

        # snapshot B's subgraph, then change + resync ONLY A
        b_before = dump(g, id_prefix=f"python://{B}")
        a.write({"a_pkg/core.py": "def compute(x, factor):\n    return x * factor\n\n\ndef extra():\n    return 0\n"})
        a2 = a.commit("a2")
        report = ingest_repo(GraphWriter(g), a, a2, cache, project=A)
        assert python_report(report).result.files_cached > 0, "expected the incremental path"

        # (a) A is correct vs a full recompute of A@a2
        ingest_repo(GraphWriter(fa), a, a2, cache, project=A)
        assert_graphs_equal(dump(fa, f"python://{A}"), dump(g, f"python://{A}"), "multi-repo A")

        # (b) B's subgraph is byte-identical — the A resync never reached across
        b_after = dump(g, id_prefix=f"python://{B}")
        assert b_before == b_after, "resyncing A must not modify B's subgraph"
    finally:
        drop(g, fa)
