"""Trigger: file save / project save in a git working tree — an *uncommitted* edit, picked
up via git (diff vs the graph's base commit + `git status` for untracked). This is the
WorkingTreeSource path; correctness is again vs a full recompute of the current on-disk state.
"""

from __future__ import annotations

import pytest

from ._helpers import (
    GitRepo, assert_graphs_equal, drop, dump, fresh_graph, ingest_dir, python_report,
)
from auryn.core.graph_client import GraphWriter

BASE = {
    "app/__init__.py": "",
    "app/util.py": "def helper(x):\n    return x + 1\n",
    "app/service.py": "from app.util import helper\n\n\ndef greet(v):\n    return helper(v)\n",
    "main.py": "from app.service import greet\n\n\ndef run():\n    return greet(1)\n",
    "extra.py": "def spare():\n    return 0\n",
}
_UTIL = "app/util.py"


@pytest.fixture
def repo(tmp_path):
    r = GitRepo.init(tmp_path / "wt").write(BASE)
    r.commit("v1")
    return r


def _ground_truth(conn, root):
    """A full recompute of the current on-disk state into a fresh graph."""
    g = fresh_graph(conn, "wt_full")
    ingest_dir(GraphWriter(g), root, project="wt")
    return g


def _resync(g, root):
    return ingest_dir(GraphWriter(g), root, project="wt")


def test_single_file_save(repo, falkordb_connection):
    g = fresh_graph(falkordb_connection, "wt")
    try:
        _resync(g, repo.root)  # first ingest (full) — stamps base_commit
        repo.write({_UTIL: "def helper(x):\n    return x + 555\n"})  # uncommitted save
        report = _resync(g, repo.root)
        assert python_report(report).result.files_cached > 0, "expected the working-tree incremental path"

        f = _ground_truth(falkordb_connection, repo.root)
        try:
            assert_graphs_equal(dump(f), dump(g), "single file save")
        finally:
            drop(f)
    finally:
        drop(g)


def test_multi_file_save(repo, falkordb_connection):
    """Project save: several files changed at once, plus a brand-new untracked file and a
    deletion — all uncommitted."""
    g = fresh_graph(falkordb_connection, "wt")
    try:
        _resync(g, repo.root)
        repo.write({
            _UTIL: "def helper(x, y):\n    return x + y\n",          # signature change
            "app/service.py": "from app.util import helper\n\n\ndef greet(v):\n    return helper(v, 2)\n",
            "app/new_mod.py": "from app.util import helper\n\n\ndef nm():\n    return helper(7, 8)\n",  # untracked
        })
        repo.rm("extra.py")                                          # deletion
        report = _resync(g, repo.root)
        assert python_report(report).result.files_cached > 0

        f = _ground_truth(falkordb_connection, repo.root)
        try:
            assert_graphs_equal(dump(f), dump(g), "multi file save")
        finally:
            drop(f)
    finally:
        drop(g)


def test_revert_uncommitted_edit(repo, falkordb_connection):
    """Edit a file, sync, then revert it back to its committed content. `git diff HEAD` no
    longer flags it, so only the remembered dirty set makes the resync undo the graph's
    divergence — the case the base_commit+dirty_paths sidecar exists for."""
    g = fresh_graph(falkordb_connection, "wt")
    try:
        _resync(g, repo.root)
        repo.write({_UTIL: "def helper(x):\n    return x + 9999\n"})
        _resync(g, repo.root)                                        # graph now has the edit
        repo.write({_UTIL: BASE[_UTIL]})                             # revert to committed content
        report = _resync(g, repo.root)
        assert python_report(report).result.files_cached > 0

        f = _ground_truth(falkordb_connection, repo.root)           # == the committed v1
        try:
            assert_graphs_equal(dump(f), dump(g), "revert uncommitted edit")
        finally:
            drop(f)
    finally:
        drop(g)


def test_commit_then_save(repo, falkordb_connection):
    """HEAD moves between syncs (an edit is committed), then a new uncommitted edit — the diff
    must still be taken against the graph's stored base, catching both."""
    g = fresh_graph(falkordb_connection, "wt")
    try:
        _resync(g, repo.root)
        repo.write({_UTIL: "def helper(x):\n    return x + 2\n"})
        _resync(g, repo.root)                                        # sync the uncommitted edit
        repo.commit("promote util change")                          # HEAD advances
        repo.write({"main.py": "from app.service import greet\n\n\ndef run():\n    return greet(1) + greet(2)\n"})
        report = _resync(g, repo.root)                              # new uncommitted edit
        assert python_report(report).result.files_cached > 0

        f = _ground_truth(falkordb_connection, repo.root)
        try:
            assert_graphs_equal(dump(f), dump(g), "commit then save")
        finally:
            drop(f)
    finally:
        drop(g)
