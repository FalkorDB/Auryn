"""Trigger: git commit — advance a branch, re-ingest at the new sha (the incremental path).

Correctness = the incrementally-synced graph is identical to a full recompute of the same
commit; plus we assert the incremental path actually ran (only the blast radius parsed).
"""

from __future__ import annotations

import pytest

from ._helpers import (
    GitRepo, assert_graphs_equal, drop, dump, fresh_graph, ingest_repo, python_report,
)
from auryn.core.graph_client import GraphWriter

# A small project with real cross-file structure: a package, inheritance, imports, and
# import-bound function calls — everything the fast `ast` engine can resolve (and therefore
# everything whose blast radius the incremental closure must get right).
BASE = {
    "app/__init__.py": "",
    "app/models.py": "class Base:\n    pass\n\n\nclass User(Base):\n    def label(self):\n        return 1\n",
    "app/util.py": "def helper(x):\n    return x + 1\n",
    "app/service.py": (
        "from app.models import User\n"
        "from app.util import helper\n\n\n"
        "def make():\n    return User()\n\n\n"
        "def greet(v):\n    return helper(v)\n"
    ),
    "main.py": (
        "from app.service import make, greet\n"
        "from app.util import helper\n\n\n"
        "def run():\n    return greet(1) + helper(2)\n"
    ),
    "standalone.py": "def solo():\n    return 0\n",  # deletable, referenced by nothing
}


@pytest.fixture
def repo(tmp_path):
    return GitRepo.init(tmp_path / "repo").write(BASE)


def _ingest(writer, repo, ref, cache):
    return ingest_repo(writer, repo, ref, cache, project="commit_proj")


def test_simple_commit(repo, falkordb_connection, tmp_path):
    """One function body changes between two commits."""
    cache = tmp_path / "cache"
    sha1 = repo.commit("v1")
    g = fresh_graph(falkordb_connection, "commit")
    f = fresh_graph(falkordb_connection, "commit_full")
    try:
        _ingest(GraphWriter(g), repo, sha1, cache)

        repo.write({"app/util.py": "def helper(x):\n    return x + 99\n"})
        sha2 = repo.commit("v2")

        report = _ingest(GraphWriter(g), repo, sha2, cache)
        assert python_report(report).result.files_cached > 0, "expected the incremental path"

        _ingest(GraphWriter(f), repo, sha2, cache)  # full recompute of v2
        assert_graphs_equal(dump(f), dump(g), "simple commit")
    finally:
        drop(g, f)


def test_mixed_commit(repo, falkordb_connection, tmp_path):
    """A mixture in one commit: a signature change (id moves, the caller's edge must follow),
    a function rename (the caller's edge must drop), a new function, a new file, and a
    deleted file — all reconciled by the incremental path in one go."""
    cache = tmp_path / "cache"
    sha1 = repo.commit("v1")
    g = fresh_graph(falkordb_connection, "commit")
    f = fresh_graph(falkordb_connection, "commit_full")
    try:
        _ingest(GraphWriter(g), repo, sha1, cache)

        repo.write({
            # signature change (helper's id changes) + a new sibling function
            "app/util.py": "def helper(x, y):\n    return x + y\n\n\ndef helper2(z):\n    return z\n",
            # rename greet -> welcome (main still calls greet -> becomes unresolved)
            "app/service.py": (
                "from app.models import User\n"
                "from app.util import helper\n\n\n"
                "def make():\n    return User()\n\n\n"
                "def welcome(v):\n    return helper(v, 1)\n"
            ),
            # a brand-new file importing + calling an existing symbol
            "app/extra.py": "from app.util import helper\n\n\ndef extra():\n    return helper(3, 4)\n",
        })
        repo.rm("standalone.py")
        sha2 = repo.commit("v2")

        report = _ingest(GraphWriter(g), repo, sha2, cache)
        py = python_report(report)
        assert py.result.files_cached > 0, "expected the incremental path"
        # the blast radius is a handful of files, not the whole repo
        assert py.result.files_parsed <= 5

        _ingest(GraphWriter(f), repo, sha2, cache)  # full recompute of v2
        assert_graphs_equal(dump(f), dump(g), "mixed commit")
    finally:
        drop(g, f)
