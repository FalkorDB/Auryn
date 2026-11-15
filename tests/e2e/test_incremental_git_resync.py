"""End-to-end: `ingest_repository` on a real git repo takes the incremental path on the
second commit (ADR 0002), and the result is identical to a full recompute at that commit.

This exercises the whole production route -- GitRepoCache clone/fetch, the sync_at routing
guard, the git-diff ChangeSet producer, and incremental_ast_sync -- not just the unit-level
oracle. It also asserts the incremental pass actually happened (only the changed + affected
files were parsed, the rest served from the graph), so a silent fall-back to full recompute
would fail the test rather than pass unnoticed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from auryn.core.graph_client import GraphWriter
from auryn.core.ingest import ingest_repository

_PROJECT = "e2e_incr"

_V1 = {
    "a.py": "def helper(x):\n    return x + 1\n",
    "b.py": "from a import helper\n\n\ndef use():\n    return helper(5)\n",
    "c.py": "def unrelated():\n    return 0\n",
    "pkg/__init__.py": "",
    "pkg/m.py": "def thing():\n    return 1\n",
}


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


def _dump(graph) -> tuple[dict, dict]:
    nodes = {
        (label, nid): {k: v for k, v in props.items() if k != "_fingerprint"}
        for label, nid, props in graph.ro_query("MATCH (n) RETURN labels(n)[0], n.id, properties(n)").result_set
    }
    edges = {
        (rel, aid, bid): {k: v for k, v in props.items() if k != "_fingerprint"}
        for rel, aid, bid, props in graph.ro_query(
            "MATCH (a)-[r]->(b) RETURN type(r), a.id, b.id, properties(r)"
        ).result_set
    }
    return nodes, edges


@pytest.fixture
def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    _write(repo, _V1)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "v1")
    return repo


def test_second_ingest_is_incremental_and_matches_full(_repo, falkordb_connection, tmp_path):
    cache = tmp_path / "cache"
    g_incr = falkordb_connection.select_graph(f"e2e_incr_{uuid4().hex}")
    g_full = falkordb_connection.select_graph(f"e2e_full_{uuid4().hex}")
    try:
        # v1: first ingest (full, into the graph we'll later resync incrementally)
        ingest_repository(GraphWriter(g_incr), str(_repo), project_name=_PROJECT,
                          cache_root=cache, deep="off", auto_install=False)

        # v2: edit a.helper's body (b.use calls it, b imports a -> both are the blast radius)
        (_repo / "a.py").write_text("def helper(x):\n    return x + 2\n")
        _git(_repo, "add", "-A")
        _git(_repo, "commit", "-qm", "v2")

        report = ingest_repository(GraphWriter(g_incr), str(_repo), project_name=_PROJECT,
                                   cache_root=cache, deep="off", auto_install=False)
        py = next(r for r in report.languages if r.language == "python")

        # The incremental path must have run: only the blast radius was parsed, the rest cached.
        assert py.result is not None
        assert py.result.files_parsed <= 3, f"expected a small blast radius, parsed {py.result.files_parsed}"
        assert py.result.files_cached >= 2, "most files should have been served from the graph, not re-parsed"

        # Full recompute of v2 into a fresh graph, and assert byte-for-byte graph equality.
        ingest_repository(GraphWriter(g_full), str(_repo), project_name=_PROJECT,
                          cache_root=cache, deep="off", auto_install=False)

        nf, ef = _dump(g_full)
        ni, ei = _dump(g_incr)
        assert nf == ni, f"nodes differ: only-full={nf.keys() - ni.keys()}, only-incr={ni.keys() - nf.keys()}"
        assert ef == ei, f"edges differ: only-full={ef.keys() - ei.keys()}, only-incr={ei.keys() - ef.keys()}"
    finally:
        for g in (g_incr, g_full):
            try:
                g.delete()
            except Exception:
                pass
