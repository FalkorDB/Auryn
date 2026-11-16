"""The per-repo sync lock (ADR 0003): `auryn sync` runs one at a time per repo, and a burst of
triggers collapses into the sync in progress plus ONE queued behind it. No graph needed."""

from __future__ import annotations

import contextlib
import queue
import threading

import pytest
from click.testing import CliRunner

from auryn.cli import cli
from auryn.core import sync_lock as sync_lock_mod
from auryn.core.sync_lock import sync_lock

pytestmark = pytest.mark.skipif(sync_lock_mod.fcntl is None, reason="no fcntl (native Windows)")


def test_a_burst_of_syncs_runs_one_queues_one_and_drops_the_rest(tmp_path, monkeypatch):
    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    turns: queue.Queue = queue.Queue()

    def trigger():
        with sync_lock(repo) as my_turn:
            turns.put(my_turn)

    with sync_lock(repo) as running:  # the sync in progress
        assert running is True
        waiters = [threading.Thread(target=trigger) for _ in range(3)]
        for t in waiters:
            t.start()
        # whichever arrives first queues (and blocks); the other two are redundant and told so now
        assert [turns.get(timeout=10), turns.get(timeout=10)] == [False, False]
        assert turns.empty()  # the queued one is still waiting for the lock we hold
    assert turns.get(timeout=10) is True  # ...and gets its turn once we are done
    for t in waiters:
        t.join(timeout=10)


def test_another_repo_is_not_held_up(tmp_path, monkeypatch):
    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "home"))
    with sync_lock(tmp_path / "a") as a, sync_lock(tmp_path / "b") as b:
        assert (a, b) == (True, True)


def test_sync_command_steps_aside_when_one_is_already_queued(tmp_path, monkeypatch):
    """A redundant `auryn sync` says so and exits before it resolves (or starts) a FalkorDB."""

    @contextlib.contextmanager
    def already_queued(local_dir):
        yield False

    def no_connection(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("a redundant sync must not resolve a connection")

    monkeypatch.setattr(sync_lock_mod, "sync_lock", already_queued)
    monkeypatch.setattr("auryn.cli._resolve_connection", no_connection)

    res = CliRunner().invoke(cli, ["sync", str(tmp_path)])

    assert res.exit_code == 0, res.output
    assert "already queued" in res.output
