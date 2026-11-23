"""Phase-2 lifecycle plumbing (ADR 0003): the config-backed ingest registry and the git
commit-resync hooks."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from auryn.core import app_config, hooks


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _init_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "a.txt").write_text("x")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


def test_config_roundtrip_trigger_and_repos(monkeypatch, tmp_path):
    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "home"))

    app_config.set_resync_trigger("save")
    app_config.register_repo(app_config.RepoRegistration(local_dir="/a", graph_name="g1", project_name="p1"))
    app_config.register_repo(app_config.RepoRegistration(local_dir="/b", graph_name="g2", trigger="commit"))

    cfg = app_config.load_config()
    assert cfg.resync_trigger == "save"
    assert {r.local_dir for r in cfg.repos} == {"/a", "/b"}
    assert app_config.get_repo("/a").graph_name == "g1"

    # upsert (same local_dir replaces, doesn't duplicate)
    app_config.register_repo(app_config.RepoRegistration(local_dir="/a", graph_name="g1b"))
    assert app_config.get_repo("/a").graph_name == "g1b"
    assert len(app_config.load_config().repos) == 2

    # per-repo trigger overrides install default; None falls back
    assert app_config.effective_trigger(app_config.get_repo("/b")) == "commit"
    assert app_config.effective_trigger(None) == "save"

    removed = app_config.unregister_repo("/a")
    assert removed.local_dir == "/a"
    assert app_config.get_repo("/a") is None
    assert {r.local_dir for r in app_config.load_config().repos} == {"/b"}


def test_hooks_install_chain_and_uninstall(tmp_path):
    repo = _init_repo(tmp_path / "r")
    d = hooks.hooks_dir(repo)

    # a pre-existing user hook must survive install + uninstall
    (d / "post-commit").write_text("#!/bin/sh\necho mine\n")
    (d / "post-commit").chmod(0o755)

    hooks.install(repo, ["true"])
    assert hooks.is_installed(repo)
    pc = (d / "post-commit").read_text()
    assert "echo mine" in pc and "auryn resync" in pc  # chained, not overwritten
    for name in hooks.HOOKS:
        assert (d / name).exists()

    # re-install replaces our block, not duplicates it
    hooks.install(repo, ["true"])
    assert (d / "post-commit").read_text().count("# >>> auryn resync >>>") == 1

    hooks.uninstall(repo)
    assert not hooks.is_installed(repo)
    assert "echo mine" in (d / "post-commit").read_text()  # user hook preserved
    assert not (d / "post-merge").exists()                 # a hook that was only ours is removed


@pytest.mark.skipif(os.name == "nt",
                    reason="git runs the generated #!/bin/sh post-commit hook through git-for-windows' bundled "
                           "sh, where a Windows backslash path in the resync command doesn't round-trip; hook "
                           "install/content/uninstall is covered by test_hooks_install_chain_and_uninstall")
def test_hook_fires_on_commit(tmp_path):
    repo = _init_repo(tmp_path / "r")
    sentinel = tmp_path / "fired"
    hooks.install(repo, ["touch", str(sentinel)])

    (repo / "b.txt").write_text("y")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "second")  # post-commit runs our block (backgrounded)

    for _ in range(60):
        if sentinel.exists():
            break
        time.sleep(0.05)
    assert sentinel.exists(), "post-commit hook did not run the resync command"
