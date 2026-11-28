"""The commit resync in linked worktrees (ADR 0003, `commit` trigger).

Linked worktrees of one repo share its hooks directory, so one hook file serves every worktree.
These tests run real `git worktree`s; the hook's interpreter is a stand-in script that records
what it was asked to sync, so no graph or server is needed.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from auryn.cli import cli
from auryn.core import lifecycle


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _repo_with_worktree(tmp_path: Path) -> tuple[Path, Path]:
    main = (tmp_path / "repo").resolve()
    main.mkdir()
    _git(main, "init", "-q", "-b", "main")
    _git(main, "config", "user.email", "t@example.com")
    _git(main, "config", "user.name", "Test")
    (main / "a.txt").write_text("x")
    _git(main, "add", "-A")
    _git(main, "commit", "-q", "-m", "init")
    other = (tmp_path / "repo-wt").resolve()
    _git(main, "worktree", "add", "-q", "-b", "wt", str(other))
    return main, other


def _commit(root: Path, name: str) -> None:
    (root / name).write_text(name)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", name)


def _recording_interpreter(tmp_path: Path) -> tuple[Path, Path]:
    """A stand-in for the interpreter the hooks name: it appends its working directory and
    arguments to a log, one line per run."""
    log = tmp_path / "runs.log"
    fake = tmp_path / "python"
    fake.write_text(f"#!/bin/sh\nprintf '%s|%s\\n' \"$(pwd -P)\" \"$*\" >> '{log}'\n")
    fake.chmod(0o755)
    return fake, log


def _wait_for_runs(log: Path, n: int) -> list[tuple[Path, list[str]]]:
    for _ in range(100):
        if log.exists() and len(log.read_text().splitlines()) >= n:
            break
        time.sleep(0.05)
    runs = []
    for line in log.read_text().splitlines() if log.exists() else []:
        cwd, args = line.split("|", 1)
        runs.append((Path(cwd), args.split()))
    return runs


@pytest.mark.parametrize("committer", ["main", "other"])
def test_a_commit_resyncs_the_worktree_it_was_made_in(monkeypatch, tmp_path, committer):
    """Regression: the shared hook named the worktree ingested last, so a commit in any other
    worktree of the repo resynced that one instead of its own."""
    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "home"))
    from auryn.core.app_config import set_resync_trigger

    main, other = _repo_with_worktree(tmp_path)
    fake, log = _recording_interpreter(tmp_path)
    monkeypatch.setattr(sys, "executable", str(fake))
    set_resync_trigger("commit")
    lifecycle.wire_trigger(str(main), "g-main")
    lifecycle.wire_trigger(str(other), "g-wt")  # last: on a shared hook file, this one's block wins
    here = {"main": main, "other": other}[committer]

    _commit(here, "b.txt")

    [(cwd, args)] = _wait_for_runs(log, 1)
    assert args[:4] == ["-m", "auryn", "sync", "--if-registered"]
    assert (cwd / args[-1]).resolve() == here


def _sync(monkeypatch, *args: str) -> list[Path]:
    """Run `auryn sync` with the ingest stubbed out; return the directories it ingested."""
    from auryn.core.graph_client import FalkorDBConfig

    ingested = []
    monkeypatch.setattr("auryn.cli._resolve_connection", lambda *a, **k: FalkorDBConfig())
    monkeypatch.setattr("auryn.cli.connect", lambda config: None)
    monkeypatch.setattr("auryn.core.ingest.ingest_directory", lambda writer, root, **k: ingested.append(Path(root)))
    monkeypatch.setattr("auryn.core.ingest.join_deep_phase", lambda: None)
    monkeypatch.setattr("auryn.cli._echo_ingest_report", lambda report: None)
    res = CliRunner().invoke(cli, ["sync", *args])
    assert res.exit_code == 0, res.output
    return ingested


def test_sync_if_registered_resyncs_only_a_registered_worktree(monkeypatch, tmp_path):
    """A worktree nobody ingested gets no graph from a commit in it -- `git worktree add` alone
    runs post-checkout there -- and leaves nothing behind in Auryn's run directory."""
    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "home"))
    from auryn.core.app_config import RepoRegistration, register_repo, set_resync_trigger
    from auryn.core.home import run_dir

    main, other = _repo_with_worktree(tmp_path)
    set_resync_trigger("commit")
    register_repo(RepoRegistration(local_dir=str(main), graph_name="g-main"))

    assert _sync(monkeypatch, "--if-registered", str(main)) == [main]
    run = sorted(run_dir().iterdir())
    assert _sync(monkeypatch, "--if-registered", str(other)) == []
    assert sorted(run_dir().iterdir()) == run  # no lock file for the unregistered worktree
    assert _sync(monkeypatch, str(other)) == [other]  # by hand, without the flag, as before


def test_sync_if_registered_honors_the_repos_own_trigger(monkeypatch, tmp_path):
    """A worktree registered with another trigger keeps it: the hook a sibling installed doesn't
    resync it on commit."""
    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "home"))
    from auryn.core.app_config import RepoRegistration, register_repo, set_resync_trigger

    main, other = _repo_with_worktree(tmp_path)
    set_resync_trigger("commit")
    register_repo(RepoRegistration(local_dir=str(main), graph_name="g-main"))
    register_repo(RepoRegistration(local_dir=str(other), graph_name="g-wt", trigger="manual"))

    assert _sync(monkeypatch, "--if-registered", str(other)) == []


def test_commit_resync_targets_take_registered_dirs_inside_the_work_tree_only(monkeypatch, tmp_path):
    """A directory registered inside the work tree (a monorepo package) is resynced by a commit
    there; a worktree placed inside it (as `.claude/worktrees/<name>`) is a work tree of its
    own, resynced by its own commits."""
    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "home"))
    from auryn.core.app_config import RepoRegistration, register_repo, set_resync_trigger

    main, _ = _repo_with_worktree(tmp_path)
    pkg = main / "packages" / "core"
    pkg.mkdir(parents=True)
    nested = main / ".claude" / "worktrees" / "x"
    _git(main, "worktree", "add", "-q", "-b", "x", str(nested))
    set_resync_trigger("commit")
    for d in (pkg, nested, tmp_path / "repo-sibling"):  # `repo-sibling`: a prefix of no work tree
        register_repo(RepoRegistration(local_dir=str(d), graph_name=d.name))

    assert lifecycle.commit_resync_targets(main) == [str(pkg)]
    assert lifecycle.commit_resync_targets(nested) == [str(nested)]
    assert lifecycle.commit_resync_targets(pkg) == [str(pkg)]


def test_forgetting_one_worktree_keeps_the_hooks_the_other_runs(monkeypatch, tmp_path):
    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "home"))
    from auryn.cli import _deregister_repo
    from auryn.core import hooks
    from auryn.core.app_config import get_repo, set_resync_trigger

    main, other = _repo_with_worktree(tmp_path)
    set_resync_trigger("commit")
    lifecycle.wire_trigger(str(main), "g-main")
    lifecycle.wire_trigger(str(other), "g-wt")

    _deregister_repo(str(other), None, None, "g-wt")
    assert get_repo(str(other)) is None
    assert hooks.is_installed(str(main))  # `main` still commits through the shared hooks

    _deregister_repo(str(main), None, None, "g-main")
    assert not hooks.is_installed(str(main))  # the last one out takes the block with it
