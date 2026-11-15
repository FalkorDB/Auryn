"""Graceful handling of broken (mid-edit) code: a save that doesn't parse must PRESERVE the
file's last-good subgraph (not wipe it), report the file as broken, and self-heal on the next
clean save. Covers Python (all-or-nothing ast SyntaxError) and TypeScript (tree-sitter
has_error) via the working-tree file-save trigger.
"""

from __future__ import annotations

import pytest

from ._helpers import GitRepo, assert_graphs_equal, drop, dump, fresh_graph, ingest_dir
from auryn.core.graph_client import GraphWriter

PY_BASE = {
    "app/__init__.py": "",
    "app/util.py": "def helper(x):\n    return x + 1\n",
    "main.py": "from app.util import helper\n\n\ndef run():\n    return helper(1)\n",
}
PY_BROKEN = "def helper(x)\n    return x + 1\n"                       # missing colon -> SyntaxError
PY_FIXED = "def helper(x):\n    return x + 2\n\n\ndef helper2(y):\n    return y\n"

TS_BASE = {
    "src/util.ts": "export function helper(x: number): number {\n  return x + 1;\n}\n",
    "src/main.ts": "import { helper } from './util';\n\n\nexport function run(): number {\n  return helper(1);\n}\n",
}
# tree-sitter-typescript sets `has_error` on TWO kinds of file it can't fully parse: a genuinely
# broken mid-edit file AND perfectly VALID modern TS its grammar doesn't model. It can't tell them
# apart, so the engine treats them uniformly by what was RECOVERED: a parse that recovered usable
# symbols is emitted best-effort (== a full ingest, no staleness); one that recovered NOTHING keeps
# the last-good subgraph. `TS_PARTIAL` recovers `helper` despite has_error; `TS_UNRECOVERABLE` recovers
# nothing.
TS_PARTIAL = "export function helper(x: number): number {\n  return x + 1;\n"       # missing } -> has_error, recovers helper
TS_UNRECOVERABLE = "export class {{{ <<< @#$ ??? \n"                                 # has_error, recovers nothing
TS_FIXED = "export function helper(x: number, y: number): number {\n  return x + y;\n}\n"


def _lang(report, lang):
    return next(r for r in report.languages if r.language == lang)


def _no_project(dumped):
    nodes, edges = dumped
    return ({k: v for k, v in nodes.items() if k[0] != "Project"}, edges)


def _roundtrip(conn, repo, rel, broken, fixed, project, lang):
    g = fresh_graph(conn, "broken")
    try:
        ingest_dir(GraphWriter(g), repo.root, project=project)      # baseline (clean)
        before = _no_project(dump(g))

        # 1) SAVE BROKEN -> last-good preserved, file reported broken
        (repo.root / rel).write_text(broken)
        report = ingest_dir(GraphWriter(g), repo.root, project=project)
        assert rel in _lang(report, lang).result.broken_files, \
            f"broken file not reported: {_lang(report, lang).result.broken_files}"
        after = _no_project(dump(g))
        assert before == after, f"{lang}: a broken save must preserve the last-good subgraph"

        # 2) SAVE FIXED -> self-heals, equals a full recompute of the fixed tree
        (repo.root / rel).write_text(fixed)
        report2 = ingest_dir(GraphWriter(g), repo.root, project=project)
        assert not _lang(report2, lang).result.broken_files

        f = fresh_graph(conn, "broken_full")
        try:
            ingest_dir(GraphWriter(f), repo.root, project=project)
            assert_graphs_equal(dump(f), dump(g), f"{lang} broken->fixed")
        finally:
            drop(f)
    finally:
        drop(g)


def test_python_broken_save_preserves_last_good(falkordb_connection, tmp_path):
    repo = GitRepo.init(tmp_path / "py").write(PY_BASE)
    repo.commit("v1")
    _roundtrip(falkordb_connection, repo, "app/util.py", PY_BROKEN, PY_FIXED, "brk_py", "python")


def test_typescript_unrecoverable_save_preserves_last_good(falkordb_connection, tmp_path):
    # A save tree-sitter recovers NOTHING usable from is treated as broken: last-good preserved,
    # reported broken, self-heals on the next clean save.
    repo = GitRepo.init(tmp_path / "ts").write(TS_BASE)
    repo.commit("v1")
    _roundtrip(falkordb_connection, repo, "src/util.ts", TS_UNRECOVERABLE, TS_FIXED, "brk_ts", "typescript")


def test_typescript_has_error_but_recoverable_updates_on_resync(falkordb_connection, tmp_path):
    # Regression: a file tree-sitter flags `has_error` but which still RECOVERS symbols (valid modern
    # TS the grammar can't fully model, e.g. zod's 5k-line schemas.ts) must UPDATE on incremental
    # resync -- the old `is_broken = has_error` kept it stale forever (a new symbol was never added).
    # It must be emitted best-effort (not reported broken) and stay == a full recompute.
    repo = GitRepo.init(tmp_path / "tshe").write({"src/m.ts": TS_PARTIAL})
    repo.commit("v1")
    g = fresh_graph(falkordb_connection, "tshe")
    f = fresh_graph(falkordb_connection, "tshe_full")
    try:
        ingest_dir(GraphWriter(g), repo.root, project="tshe")                     # baseline (has_error)
        assert any("helper" in k[1] for k in dump(g)[0]), "a has_error file that recovers symbols must be emitted, not skipped"

        # add a second symbol (file still has_error) and resync incrementally
        (repo.root / "src/m.ts").write_text(
            "export function helper(x: number): number {\n  return x + 1;\n}\n"
            "export function helper2(y: number): number {\n  return y;\n"          # still missing final }
        )
        ingest_dir(GraphWriter(g), repo.root, project="tshe")                     # incremental resync
        assert any("helper2" in k[1] for k in dump(g)[0]), \
            "resync must add the new symbol -- the fix: a has_error file no longer goes stale"

        # the incrementally-resynced graph equals a full recompute of the same (still has_error) state
        ingest_dir(GraphWriter(f), repo.root, project="tshe")
        assert_graphs_equal(dump(f), dump(g), "ts has_error recoverable: incremental == full")
    finally:
        drop(g)
        drop(f)
