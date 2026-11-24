"""Phase-2 lifecycle plumbing (ADR 0003): the config-backed ingest registry and the git
commit-resync hooks."""

from __future__ import annotations

import os
import shutil
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
                    reason="git runs the #!/bin/sh hook through git-for-windows' bundled sh, where the Windows "
                           "backslash path in it doesn't round-trip (as for test_hook_fires_on_commit)")
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


def test_a_wait_in_the_users_hook_does_not_wait_for_the_resync(tmp_path):
    """Ahead of the user's body, our backgrounded line was one more job of the hook's shell: a
    hook that ends in `wait` (for its own jobs) waited for the resync too, so every commit took
    as long as the sync. The block backgrounds it from a subshell -- nothing for `wait` to find."""
    repo = _init_repo(tmp_path / "r")
    d = hooks.hooks_dir(repo)
    (d / "post-commit").write_text("#!/bin/sh\ntrue &\nwait\n")
    (d / "post-commit").chmod(0o755)
    hooks.install(repo, ["sleep", "5"])  # stands in for a slow resync

    (repo / "b.txt").write_text("y")
    _git(repo, "add", "-A")
    started = time.monotonic()
    _git(repo, "commit", "-q", "-m", "second")
    assert time.monotonic() - started < 3, "the commit waited for the resync"


def test_a_block_without_its_end_marker_does_not_take_the_users_lines_with_it(tmp_path, monkeypatch):
    """The end marker deleted by hand: everything after the begin marker read as our block. With
    the block ahead of the user's body that is the user's own code -- uninstall saw a bare
    `#!/bin/sh` left over and deleted the hook. A begin marker with no end bounds nothing now: the
    hook is left as it is, and a re-install adds one whole block without dropping a line. The line
    under that marker may still run a sync, so uninstall says so instead of reporting a clean sweep."""
    repo = _init_repo(tmp_path / "r")
    hook = hooks.hooks_dir(repo) / "post-commit"
    broken = "#!/bin/sh\n# >>> auryn resync >>>\n(true >/dev/null 2>&1 &)\necho user-code\n"
    hook.write_text(broken)
    hook.chmod(0o755)

    with pytest.raises(ValueError, match="without its end marker"):
        hooks.uninstall(repo)
    assert hook.read_text() == broken

    hooks.install(repo, ["true"])
    hooks.install(repo, ["true"])
    assert hook.read_text().count("# <<< auryn resync <<<") == 1 and "echo user-code" in hook.read_text()
    with pytest.raises(ValueError, match="without its end marker"):
        hooks.uninstall(repo)
    assert hook.read_text() == broken

    from auryn.cli import _deregister_repo

    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "home"))
    app_config.register_repo(app_config.RepoRegistration(local_dir=str(repo)))
    _deregister_repo(str(repo), None, None, "g")  # `auryn forget`: says so, and keeps the repo on record
    assert app_config.get_repo(str(repo))


@pytest.mark.skipif(os.name == "nt", reason="native Windows has no exec bit: chmod 644 can't switch a hook off there")
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


def test_install_takes_an_older_versions_block_out_of_a_hook_it_now_skips(tmp_path):
    """Upgrading: earlier versions appended the block to ANY hook -- a Python one (a SyntaxError on
    every commit) and one under `core.hooksPath` (a tracked file). Only skipping those on the next
    install would leave them broken for good, so the skip also takes the old block back out."""
    repo = _init_repo(tmp_path / "r")
    d = hooks.hooks_dir(repo)
    old_block = "# >>> auryn resync >>>\n/old/python -m auryn sync /r >/dev/null 2>&1 &\n# <<< auryn resync <<<\n"
    python_hook = "#!/usr/bin/env python3\nprint('mine')\n"
    (d / "post-commit").write_text(python_hook + old_block)
    (d / "post-commit").chmod(0o755)

    assert "post-commit" not in hooks.install(repo, ["true"])
    assert (d / "post-commit").read_text() == python_hook

    husky = repo / ".husky"
    husky.mkdir()
    (husky / "post-commit").write_text("#!/bin/sh\nnpm test\n" + old_block)
    (husky / "post-merge").write_text("#!/bin/sh\n" + old_block)  # only ever ours
    _git(repo, "config", "core.hooksPath", ".husky")

    assert hooks.install(repo, ["true"]) == []
    assert (husky / "post-commit").read_text() == "#!/bin/sh\nnpm test\n"
    assert not (husky / "post-merge").exists()


def test_install_never_writes_through_a_symlinked_hook(tmp_path):
    """`.git/hooks/post-commit -> ../../scripts/post-commit` (a tracked script linked into place):
    writing the block there edits the tracked file through the link. A symlinked hook is left
    alone, and a dangling one doesn't get its target created."""
    repo = _init_repo(tmp_path / "r")
    d = hooks.hooks_dir(repo)
    tracked = repo / "scripts" / "post-commit"
    tracked.parent.mkdir()
    tracked.write_text("#!/bin/sh\necho mine\n")
    tracked.chmod(0o755)
    (d / "post-commit").symlink_to(tracked)
    (d / "post-merge").symlink_to(repo / "scripts" / "gone")

    wired = hooks.install(repo, ["true"])

    assert wired == ["post-checkout", "post-rewrite"]
    assert tracked.read_text() == "#!/bin/sh\necho mine\n"
    assert not (repo / "scripts" / "gone").exists()


@pytest.mark.parametrize("link, to", [("post-merge", "post-commit"), ("post-commit", "post-merge")])
def test_a_hook_linked_to_another_hook_keeps_the_block(tmp_path, link, to):
    """`post-merge -> post-commit`: one script for both events. The link has no text of its own,
    so reading it showed the block install had just put in the other hook -- which looked like an
    old block to take back out, and was stripped again. The status said installed and no commit
    ever resynced. Whichever of the two comes first in HOOKS."""
    repo = _init_repo(tmp_path / "r")
    d = hooks.hooks_dir(repo)
    (d / to).write_text("#!/bin/sh\necho mine\n")
    (d / to).chmod(0o755)
    (d / link).symlink_to(to)

    assert hooks.install(repo, ["true"]) == list(hooks.HOOKS)  # the link counts through its target
    assert "auryn resync" in (d / to).read_text() and (d / link).is_symlink()

    hooks.uninstall(repo)
    assert (d / to).read_text() == "#!/bin/sh\necho mine\n" and (d / link).is_symlink()


def test_a_hook_we_may_not_read_is_left_alone(tmp_path, monkeypatch):
    """`chmod 000` on a hook: install and the status check raised PermissionError. It is skipped,
    like a compiled hook. (The refusal is simulated, so this holds when run as root too.)"""
    repo = _init_repo(tmp_path / "r")
    locked = hooks.hooks_dir(repo) / "post-commit"
    locked.write_text("#!/bin/sh\necho mine\n")
    locked.chmod(0o755)
    real_open = Path.open

    def refuses_the_locked_hook(self, *args, **kwargs):
        if self == locked:
            raise PermissionError(13, "Permission denied", str(self))
        return real_open(self, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(Path, "open", refuses_the_locked_hook)
        assert hooks.install(repo, ["true"]) == ["post-checkout", "post-merge", "post-rewrite"]
        assert hooks.is_installed(repo)
        hooks.uninstall(repo)
    assert locked.read_text() == "#!/bin/sh\necho mine\n"


def test_a_hook_that_links_to_itself_does_not_stop_the_install(tmp_path):
    """`post-merge -> post-merge`: resolving such a link raises on Python 3.12. It is skipped like
    any other dangling link."""
    repo = _init_repo(tmp_path / "r")
    loop = hooks.hooks_dir(repo) / "post-merge"
    loop.symlink_to("post-merge")

    assert hooks.install(repo, ["true"]) == ["post-commit", "post-checkout", "post-rewrite"]
    hooks.uninstall(repo)
    assert loop.is_symlink()


def test_a_crlf_hook_keeps_its_line_endings(tmp_path):
    """A hook saved with CRLF line endings came back LF once the block went in or out -- every
    line of a hook that wasn't ours to reformat. Its own lines are written back as they were read."""
    repo = _init_repo(tmp_path / "r")
    path = hooks.hooks_dir(repo) / "post-commit"
    path.write_bytes(b"#!/bin/sh\r\necho mine\r\n")
    path.chmod(0o755)

    hooks.install(repo, ["true"])
    assert path.read_bytes().startswith(b"#!/bin/sh\r\n# >>> auryn resync >>>")
    assert path.read_bytes().endswith(b"echo mine\r\n")

    hooks.uninstall(repo)
    assert path.read_bytes() == b"#!/bin/sh\r\necho mine\r\n"


def test_install_refuses_a_hooks_dir_that_is_a_symlink(tmp_path):
    """`.git/hooks -> ../githooks`: the older way to share tracked hooks, before `core.hooksPath`.
    It is refused like a redirected hooks path -- the block would land in the tracked directory."""
    repo = _init_repo(tmp_path / "r")
    shared = repo / "githooks"
    shared.mkdir()
    hooks_link = hooks.hooks_dir(repo)
    shutil.rmtree(hooks_link)
    hooks_link.symlink_to(shared)

    assert hooks.install(repo, ["true"]) == []
    assert list(shared.iterdir()) == []


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
    assert not hooks.is_installed(repo)  # ...and the status check reads past the compiled hook

    hooks.install(repo, ["true"])  # install skips the compiled hook too, instead of crashing on it
    hooks.uninstall(repo)
    assert (d / "post-merge").read_bytes() == before["post-merge"]


def test_uninstall_removes_the_hooks_of_every_registered_repo(monkeypatch, tmp_path):
    """`auryn uninstall` reverses install: a hook left behind would run `auryn sync` on the next
    commit and recreate the home dir that was just removed."""
    from auryn.core.install import do_uninstall

    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "home"))
    repos = [_init_repo(tmp_path / name) for name in ("a", "b")]
    users_hook = hooks.hooks_dir(repos[1]) / "post-commit"
    users_hook.write_text("#!/bin/sh\necho mine\n")
    users_hook.chmod(0o755)
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
    assert users_hook.read_text() == "#!/bin/sh\necho mine\n"  # ...whose own hook body stays
    assert not (tmp_path / "home").exists()  # ...or the rest of the teardown


def test_a_hook_we_may_not_write_does_not_end_the_uninstall_sweep(monkeypatch, tmp_path):
    """A read-only post-commit raised out of the loop: the hooks after it kept their block, and
    `forget` dropped the repo from the registry all the same -- nothing left to retry from. The
    other hooks are cleaned first, and the repo stays registered. (The refusal is simulated, so
    this holds when run as root too.)"""
    from auryn.cli import _deregister_repo
    from auryn.core.install import do_uninstall

    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "home"))
    repo = _init_repo(tmp_path / "r")
    d = hooks.hooks_dir(repo)
    locked = d / "post-commit"
    locked.write_text("#!/bin/sh\necho mine\n")
    locked.chmod(0o755)
    app_config.register_repo(app_config.RepoRegistration(local_dir=str(repo)))
    hooks.install(repo, ["true"])
    real_write_text = Path.write_text

    def refuses_the_locked_hook(self, *args, **kwargs):
        if self == locked:
            raise PermissionError(13, "Permission denied", str(self))
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", refuses_the_locked_hook)

    with pytest.raises(PermissionError):
        hooks.uninstall(repo)
    assert [n for n in hooks.HOOKS if (d / n).exists()] == ["post-commit"]  # the rest are cleaned

    _deregister_repo(str(repo), None, None, "g")  # `auryn forget`
    assert app_config.get_repo(str(repo))
    app_config.set_resync_trigger("save")
    do_uninstall(adapters=[])
    assert app_config.get_repo(str(repo))
    assert app_config.load_config().resync_trigger == "save"  # the rest of config.toml stays as it was


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
