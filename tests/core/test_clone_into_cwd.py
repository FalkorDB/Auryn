"""`ingest repo <remote-uri>` clones into the CWD (like `git clone`) and ingests that working
checkout, so save/commit triggers attach to a real local copy (ADR 0003)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from click.testing import CliRunner

from auryn.cli import cli
from auryn.core import hooks
from auryn.core.app_config import get_repo
from auryn.core.graph_client import FalkorDBConfig, connect


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def test_ingest_repo_clones_into_cwd(falkordb_connection, tmp_path, monkeypatch):
    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "home"))

    # a source repo to clone FROM (a remote-style file:// URI, not an existing local dir path)
    src = tmp_path / "src"
    src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    _git(src, "config", "user.email", "t@example.com")
    _git(src, "config", "user.name", "Test")
    (src / "util.py").write_text("def helper(x):\n    return x + 1\n")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "v1")

    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)

    res = CliRunner().invoke(
        cli,
        ["ingest", "--host", "localhost", "--port", "6379", "repo", f"file://{src}",
         "--trigger", "commit", "--deep", "off"],
        catch_exceptions=False,
    )
    assert res.exit_code == 0, res.output

    dest = work / "src"
    assert dest.is_dir() and (dest / ".git").exists(), "should have cloned into CWD/src"

    reg = get_repo(str(dest.resolve()))
    assert reg is not None and reg.graph_name, "clone should be registered"
    assert hooks.is_installed(dest), "commit hooks should be installed in the clone"

    g = connect(FalkorDBConfig(host="localhost", port=6379, graph_name=reg.graph_name))
    try:
        assert _count(g, "MATCH (f:Function) RETURN count(f)") >= 1, "graph should be populated"
        # base_commit stamped (working-tree source) so the next save is incremental
        base = g.ro_query("MATCH (p:Project) RETURN p.base_commit").result_set
        assert base and base[0][0], "working-tree base_commit should be stamped"
    finally:
        g.delete()


def _count(g, cypher: str) -> int:
    return g.ro_query(cypher).result_set[0][0]
