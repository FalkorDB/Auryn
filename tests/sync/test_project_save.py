"""Trigger: project save in a plain (non-git) directory. Per policy this is NOT incremental —
there's no git to name the changed set — so it runs a full resync. The test locks in that the
full-resync path stays *correct* across simple and mixed local edits (the graph after a
re-ingest equals a fresh full ingest of the final state), and confirms it did NOT sneak onto
the incremental path (no working-tree base was recorded).
"""

from __future__ import annotations

import pytest

from ._helpers import assert_graphs_equal, drop, dump, fresh_graph, ingest_dir, python_report
from auryn.core.graph_client import GraphWriter

BASE = {
    "app/__init__.py": "",
    "app/util.py": "def helper(x):\n    return x + 1\n",
    "main.py": "from app.util import helper\n\n\ndef run():\n    return helper(1)\n",
    "gone.py": "def gone():\n    return 0\n",
}


@pytest.fixture
def plain_dir(tmp_path):
    root = tmp_path / "proj"  # NOT a git repo
    for rel, content in BASE.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return root


def _write(root, files):
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


def test_non_git_project_save_full_resync_correct(plain_dir, falkordb_connection):
    g = fresh_graph(falkordb_connection, "proj")
    f = fresh_graph(falkordb_connection, "proj_full")
    try:
        ingest_dir(GraphWriter(g), plain_dir, project="proj")

        # a mixture of local edits, no git involved
        _write(plain_dir, {
            "app/util.py": "def helper(x, y):\n    return x + y\n",     # signature change
            "app/added.py": "from app.util import helper\n\n\ndef a():\n    return helper(1, 2)\n",
        })
        (plain_dir / "gone.py").unlink()                                # deletion

        report = ingest_dir(GraphWriter(g), plain_dir, project="proj")
        # full resync (no git) -> nothing cached, and no working-tree base recorded
        assert report.commit_sha  # a tree hash, not a commit
        assert python_report(report).result.files_cached == 0, "a non-git dir must take the full path"

        ingest_dir(GraphWriter(f), plain_dir, project="proj")           # fresh full ingest of final state
        assert_graphs_equal(dump(f), dump(g), "non-git project save")
    finally:
        drop(g, f)
