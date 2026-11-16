"""Phase-2 lifecycle plumbing (ADR 0003): the config-backed ingest registry and the git
commit-resync hooks."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

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


def test_hook_block_runs_even_when_the_users_hook_exits_first(tmp_path):
    """A user hook that ends in `exit 0` (or `exec`) used to end before our appended block ever
    ran. The block goes right after the shebang now, ahead of the user's body."""
    repo = _init_repo(tmp_path / "r")
    d = hooks.hooks_dir(repo)
    mine = tmp_path / "mine"
    (d / "post-commit").write_text(f"#!/bin/sh\ntouch {mine}\nexit 0\n")
    (d / "post-commit").chmod(0o755)
    sentinel = tmp_path / "fired"
    hooks.install(repo, ["touch", str(sentinel)])

    (repo / "b.txt").write_text("y")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "second")

    for _ in range(60):
        if sentinel.exists():
            break
        time.sleep(0.05)
    assert sentinel.exists(), "the resync block did not run ahead of the user's `exit 0`"
    assert mine.exists()  # ...and the user's own hook still ran


def test_install_leaves_a_non_shell_hook_and_a_disabled_hook_as_they_are(tmp_path):
    """A shell line appended to a Python hook is a SyntaxError that also stops the USER's hook;
    and `chmod 755` on a hook the user had switched off switches it back on. Neither is ours to
    change: the non-shell hook is skipped, an existing hook keeps its mode."""
    repo = _init_repo(tmp_path / "r")
    d = hooks.hooks_dir(repo)
    python_hook = "#!/usr/bin/env python3\nprint('mine')\n"
    (d / "post-commit").write_text(python_hook)
    (d / "post-commit").chmod(0o755)
    (d / "post-merge").write_text("#!/bin/sh\necho mine\n")
    (d / "post-merge").chmod(0o644)  # disabled by its owner

    wired = hooks.install(repo, ["true"])

    assert wired == ["post-checkout", "post-rewrite"]  # the hooks that will actually run it
    assert (d / "post-commit").read_text() == python_hook
    assert (d / "post-merge").stat().st_mode & 0o777 == 0o644
    assert (d / "post-merge").read_text().startswith("#!/bin/sh\n# >>> auryn resync >>>")


def test_install_refuses_a_hooks_dir_outside_the_repos_git_dir(tmp_path):
    """`core.hooksPath` -> a TRACKED dir (husky's `.husky/`, `.githooks/`): our block, carrying this
    machine's interpreter path, would land in the work tree. (A machine-wide hooksPath is the same
    refusal: every repo's commit would resync the last repo ingested.)"""
    repo = _init_repo(tmp_path / "r")
    _git(repo, "config", "core.hooksPath", ".husky")

    assert hooks.install(repo, ["true"]) == []
    assert not (repo / ".husky").exists()
    assert not hooks.is_installed(repo)
    status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True)
    assert status.stdout == ""  # the work tree is untouched


def test_uninstall_leaves_a_hook_without_our_block_byte_for_byte(tmp_path):
    """The uninstall sweep reaches every registered repo -- also one that never got our hooks (a
    `save` / `manual` trigger, a refused hooks dir). A hook that isn't ours must not be rewritten,
    and a user's bare `#!/bin/sh` placeholder must not be deleted as if it had been ours."""
    repo = _init_repo(tmp_path / "r")
    d = hooks.hooks_dir(repo)
    (d / "post-commit").write_text("#!/bin/sh\n")
    (d / "post-merge").write_bytes(b"\x7fELF\xff\xfe compiled hook")
    before = {n: (d / n).read_bytes() for n in ("post-commit", "post-merge")}

    hooks.uninstall(repo)
    assert {n: (d / n).read_bytes() for n in before} == before

    hooks.install(repo, ["true"])  # install skips the compiled hook too, instead of crashing on it
    hooks.uninstall(repo)
    assert (d / "post-merge").read_bytes() == before["post-merge"]


def test_uninstall_removes_the_hooks_of_every_registered_repo(monkeypatch, tmp_path):
    """`auryn uninstall` reverses install: a hook left behind would run `auryn sync` on the next
    commit and recreate the home dir that was just removed."""
    from auryn.core.install import do_uninstall

    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "home"))
    repos = [_init_repo(tmp_path / name) for name in ("a", "b")]
    for repo in repos:
        app_config.register_repo(app_config.RepoRegistration(local_dir=str(repo)))
        hooks.install(repo, ["true"])
        assert hooks.is_installed(repo)

    real_uninstall = hooks.uninstall

    def fails_on_the_first(local_dir):  # e.g. a hook file it may not write
        if str(local_dir) == str(repos[0]):
            raise PermissionError("read-only hook")
        real_uninstall(local_dir)

    monkeypatch.setattr(hooks, "uninstall", fails_on_the_first)

    do_uninstall(purge=True, adapters=[])

    assert not hooks.is_installed(repos[1])  # one repo failing doesn't strand the next...
    assert not (tmp_path / "home").exists()  # ...or the rest of the teardown


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
