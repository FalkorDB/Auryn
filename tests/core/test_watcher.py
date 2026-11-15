"""Save-trigger watcher (ADR 0003): routing/registry helpers + an end-to-end edit→resync."""

from __future__ import annotations

import importlib.util
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from auryn.core import app_config, watcher

_HAS_WATCHDOG = importlib.util.find_spec("watchdog") is not None


def _server_up(host: str = "localhost", port: int = 6379) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


_LOCAL_SERVER = _server_up()


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def test_owning_repo_longest_prefix(tmp_path):
    a = str((tmp_path / "a").resolve())
    ab = str((tmp_path / "a" / "b").resolve())
    roots = {a, ab}
    assert watcher._owning_repo(str(tmp_path / "a" / "b" / "x.py"), roots) == ab  # nested -> deepest
    assert watcher._owning_repo(str(tmp_path / "a" / "y.py"), roots) == a
    assert watcher._owning_repo(str(tmp_path / "z" / "q.py"), roots) is None


def test_save_repo_dirs_filters(monkeypatch, tmp_path):
    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "home"))
    d1 = tmp_path / "r1"; d1.mkdir()
    d2 = tmp_path / "r2"; d2.mkdir()
    app_config.set_resync_trigger("save")
    app_config.register_repo(app_config.RepoRegistration(local_dir=str(d1), graph_name="g1"))  # inherits save
    app_config.register_repo(app_config.RepoRegistration(local_dir=str(d2), graph_name="g2", trigger="commit"))
    app_config.register_repo(app_config.RepoRegistration(local_dir=str(tmp_path / "gone"), graph_name="g3"))  # missing
    assert watcher._save_repo_dirs() == {str(d1.resolve())}


def test_suffix_and_exclude_sets():
    suf = watcher._code_suffixes()
    assert ".py" in suf and ".ts" in suf
    assert {".git", "node_modules"} <= watcher._excluded_dirs()


@pytest.mark.skipif(not _HAS_WATCHDOG, reason="watchdog not installed (auto-installed on demand in production)")
@pytest.mark.skipif(not _LOCAL_SERVER, reason="needs a shared FalkorDB on localhost:6379 -- the watcher "
                                              "spawns a real `auryn sync` subprocess that reaches the "
                                              "graph only through config.toml, so it cannot share the "
                                              "in-process embedded test backend")
def test_watcher_resyncs_on_save(tmp_path, monkeypatch):
    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "home"))
    from auryn.core.graph_client import FalkorDBConfig, GraphWriter, connect
    from auryn.core.ingest import ingest_directory
    from auryn.core.project import default_graph_name

    # The spawned `sync` subprocess connects via config.toml, so `g` must point at the SAME
    # server (localhost:6379) -- not the fixture's in-process embedded backend.
    app_config.save_config(app_config.Config(
        backend=app_config.Backend(mode="remote", host="localhost", port=6379), resync_trigger="save"))

    repo = tmp_path / "repo"; repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "util.py").write_text("def helper(x):\n    return x + 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v1")

    graph = default_graph_name(repo)
    app_config.register_repo(app_config.RepoRegistration(local_dir=str(repo.resolve()), graph_name=graph))
    g = connect(FalkorDBConfig(host="localhost", port=6379, graph_name=graph))
    ingest_directory(GraphWriter(g), str(repo), deep="off", auto_install=False)
    assert g.ro_query("MATCH (f:Function) RETURN count(f)").result_set[0][0] == 1

    stop = threading.Event()
    th = threading.Thread(target=watcher.run_watcher,
                          kwargs={"stop_event": stop, "debounce": 0.2, "reconcile_every": 1.0}, daemon=True)
    th.start()
    try:
        time.sleep(0.6)  # let the observer attach
        (repo / "util.py").write_text("def helper(x):\n    return x + 2\n\n\ndef helper2(y):\n    return y\n")
        after = 1
        for _ in range(150):  # up to ~15s: debounce + python startup + resync
            after = g.ro_query("MATCH (f:Function) RETURN count(f)").result_set[0][0]
            if after != 1:
                break
            time.sleep(0.1)
        assert after == 2, f"watcher did not resync on save (functions={after})"
    finally:
        stop.set()
        th.join(timeout=5)
        g.delete()
