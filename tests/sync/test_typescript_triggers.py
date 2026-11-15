"""TypeScript across the real ingest path: a git-commit resync and a working-tree file-save,
each taking the incremental path and landing on a graph identical to a full recompute. Proves
the shared engine + git routing work for TS end-to-end (not just the unit oracle).
"""

from __future__ import annotations

import pytest

from ._helpers import GitRepo, assert_graphs_equal, drop, dump, fresh_graph, ingest_dir, ingest_repo
from auryn.core.graph_client import GraphWriter

BASE = {
    "src/util.ts": "export function helper(x: number): number {\n  return x + 1;\n}\n",
    "src/service.ts": "import { helper } from './util';\n\n\nexport function greet(v: number): number {\n  return helper(v);\n}\n",
    "src/main.ts": "import { greet } from './service';\n\n\nexport function run(): number {\n  return greet(1);\n}\n",
    "src/standalone.ts": "export function solo(): number {\n  return 0;\n}\n",
}


def _ts(report):
    return next(r for r in report.languages if r.language == "typescript")


@pytest.fixture
def repo(tmp_path):
    return GitRepo.init(tmp_path / "ts").write(BASE)


def test_git_commit_incremental(repo, falkordb_connection, tmp_path):
    cache = tmp_path / "cache"
    sha1 = repo.commit("v1")
    g = fresh_graph(falkordb_connection, "ts")
    f = fresh_graph(falkordb_connection, "ts_full")
    try:
        ingest_repo(GraphWriter(g), repo, sha1, cache, project="ts_proj")

        repo.write({  # a mixture in one commit
            "src/util.ts": "export function helper(x: number, y: number): number {\n  return x + y;\n}\n",
            "src/extra.ts": "import { helper } from './util';\n\n\nexport function extra(): number {\n  return helper(3, 4);\n}\n",
        })
        repo.rm("src/standalone.ts")
        sha2 = repo.commit("v2")

        report = ingest_repo(GraphWriter(g), repo, sha2, cache, project="ts_proj")
        assert _ts(report).result.files_cached > 0, "expected the TS incremental path"

        ingest_repo(GraphWriter(f), repo, sha2, cache, project="ts_proj")
        assert_graphs_equal(dump(f), dump(g), "TS git commit")
    finally:
        drop(g, f)


def test_file_save_incremental(repo, falkordb_connection):
    repo.commit("v1")
    g = fresh_graph(falkordb_connection, "ts")
    try:
        ingest_dir(GraphWriter(g), repo.root, project="ts_ws")  # first ingest (full)
        (repo.root / "src/util.ts").write_text("export function helper(x: number): number {\n  return x + 555;\n}\n")
        report = ingest_dir(GraphWriter(g), repo.root, project="ts_ws")  # uncommitted save
        assert _ts(report).result.files_cached > 0, "expected the TS working-tree incremental path"

        f = fresh_graph(falkordb_connection, "ts_full")
        try:
            ingest_dir(GraphWriter(f), repo.root, project="ts_ws")  # full recompute of current worktree
            assert_graphs_equal(dump(f), dump(g), "TS file save")
        finally:
            drop(f)
    finally:
        drop(g)
