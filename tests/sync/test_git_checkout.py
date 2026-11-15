"""Trigger: git checkout — move the graph to a *different branch's* tip (not a linear
advance). The changed set is the net diff between the two branch tips, which the incremental
path handles the same way as a commit; correctness is again vs a full recompute of the tip.
"""

from __future__ import annotations

import pytest

from ._helpers import (
    GitRepo, assert_graphs_equal, drop, dump, fresh_graph, ingest_repo, python_report,
)
from auryn.core.graph_client import GraphWriter

BASE = {
    "app/__init__.py": "",
    "app/util.py": "def helper(x):\n    return x + 1\n",
    "app/service.py": "from app.util import helper\n\n\ndef greet(v):\n    return helper(v)\n",
    "main.py": "from app.service import greet\n\n\ndef run():\n    return greet(1)\n",
}


@pytest.fixture
def diverged(tmp_path):
    """A repo with `main` and `feature` diverged from a common base."""
    repo = GitRepo.init(tmp_path / "repo").write(BASE)
    repo.commit("base")
    repo.checkout("feature", create=True)
    repo.write({
        "app/service.py": "from app.util import helper\n\n\ndef welcome(v):\n    return helper(v) + 1\n",
        "app/feature_only.py": "from app.util import helper\n\n\ndef fx():\n    return helper(9)\n",
    })
    repo.commit("feature work")
    repo.checkout("main")
    repo.write({"app/util.py": "def helper(x):\n    return x + 42\n\n\ndef helper2(y):\n    return y\n"})
    repo.commit("main work")
    return repo


def _ingest(writer, repo, ref, cache):
    return ingest_repo(writer, repo, ref, cache, project="checkout_proj")


def test_checkout_to_other_branch(diverged, falkordb_connection, tmp_path):
    cache = tmp_path / "cache"
    g = fresh_graph(falkordb_connection, "checkout")
    f = fresh_graph(falkordb_connection, "checkout_full")
    try:
        _ingest(GraphWriter(g), diverged, "main", cache)                 # graph at main tip
        report = _ingest(GraphWriter(g), diverged, "feature", cache)     # checkout -> feature
        assert python_report(report).result.files_cached > 0, "expected the incremental path"

        _ingest(GraphWriter(f), diverged, "feature", cache)              # full recompute of feature
        assert_graphs_equal(dump(f), dump(g), "checkout main->feature")
    finally:
        drop(g, f)


def test_checkout_round_trip(diverged, falkordb_connection, tmp_path):
    """main -> feature -> back to main; each hop is incremental and must match a full recompute
    of wherever we landed (so a branch bounce can never leave residue behind)."""
    cache = tmp_path / "cache"
    g = fresh_graph(falkordb_connection, "checkout")
    f = fresh_graph(falkordb_connection, "checkout_full")
    try:
        _ingest(GraphWriter(g), diverged, "main", cache)
        _ingest(GraphWriter(g), diverged, "feature", cache)
        report = _ingest(GraphWriter(g), diverged, "main", cache)        # back to main
        assert python_report(report).result.files_cached > 0

        _ingest(GraphWriter(f), diverged, "main", cache)
        assert_graphs_equal(dump(f), dump(g), "checkout round-trip")
    finally:
        drop(g, f)
