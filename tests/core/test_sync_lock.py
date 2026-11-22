"""The per-repo sync lock (ADR 0003): `auryn sync` runs one at a time per repo, and a burst of
triggers collapses into the sync in progress plus ONE queued behind it. No graph needed."""

from __future__ import annotations

import contextlib
import queue
import threading
import time

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
        waiters = [threading.Thread(target=trigger, daemon=True) for _ in range(3)]
        for t in waiters:
            t.start()
        # whichever arrives first queues (and blocks); the other two are redundant and told so now
        assert [turns.get(timeout=10), turns.get(timeout=10)] == [False, False]
        assert turns.empty()  # the queued one is still waiting for the lock we hold
    assert turns.get(timeout=10) is True  # ...and gets its turn once we are done
    for t in waiters:
        t.join(timeout=10)
    assert not any(t.is_alive() for t in waiters)


def test_a_sync_the_queued_one_would_not_cover_waits_instead_of_stepping_aside(tmp_path, monkeypatch):
    """`queue_one=False` (a deep pass, another server): never told it is redundant -- it runs."""
    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    turns: queue.Queue = queue.Queue()

    def trigger(queue_one):
        with sync_lock(repo, queue_one=queue_one) as my_turn:
            turns.put(my_turn)

    with sync_lock(repo):
        waiters = [threading.Thread(target=trigger, args=(q,), daemon=True) for q in (True, False, False)]
        for t in waiters:
            t.start()
        with pytest.raises(queue.Empty):  # one queued, two waiting -- nobody steps aside
            turns.get(timeout=0.5)
    assert [turns.get(timeout=10) for _ in waiters] == [True, True, True]  # each got its own turn
    for t in waiters:
        t.join(timeout=10)
    assert not any(t.is_alive() for t in waiters)


def test_a_waiting_sync_says_what_it_is_waiting_for(tmp_path, monkeypatch, caplog):
    """Blocking with no word looked like a hang -- `auryn ingest dir` behind a hook's sync, say."""
    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    done = threading.Event()

    def waiter():
        with sync_lock(repo, queue_one=False):
            done.set()

    with caplog.at_level("INFO", logger="auryn.core.sync_lock"), sync_lock(repo):
        t = threading.Thread(target=waiter, daemon=True)
        t.start()
        for _ in range(100):  # until the waiter has announced itself (it is blocked on our lock)
            if caplog.records:
                break
            time.sleep(0.05)
        assert [r.getMessage() for r in caplog.records] == [f"Waiting for the running sync of {repo} to finish"]
        assert not done.is_set()
    assert done.wait(timeout=10)
    t.join(timeout=10)
    assert len(caplog.records) == 1  # our own, uncontended lock said nothing


def test_another_repo_is_not_held_up(tmp_path, monkeypatch):
    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "home"))
    with sync_lock(tmp_path / "a") as a, sync_lock(tmp_path / "b") as b:
        assert (a, b) == (True, True)


def test_a_very_long_directory_name_still_fits_a_lock_file_name(tmp_path, monkeypatch):
    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "home"))
    with sync_lock(tmp_path / ("r" * 240)) as my_turn:  # with "sync-", the hash and ".queued": over 255 bytes
        assert my_turn


@pytest.mark.parametrize("args, may_step_aside", [([], True), (["--deep", "inline"], False), (["--port", "6400"], False)])
def test_sync_command_steps_aside_only_for_a_plain_sync(tmp_path, monkeypatch, args, may_step_aside):
    """A redundant plain `auryn sync` (what a hook runs) says so and exits before it resolves --
    or starts -- a FalkorDB. One that asks for more than the queued sync will do (a deep pass,
    another server) tells the lock it must not be skipped."""
    asked = []

    @contextlib.contextmanager
    def already_queued(local_dir, *, queue_one=True):
        asked.append(queue_one)
        yield False

    def no_connection(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("a redundant sync must not resolve a connection")

    monkeypatch.setattr(sync_lock_mod, "sync_lock", already_queued)
    monkeypatch.setattr("auryn.cli._resolve_connection", no_connection)

    res = CliRunner().invoke(cli, ["sync", str(tmp_path), *args])

    assert res.exit_code == 0, res.output
    assert "already queued" in res.output
    assert asked == [may_step_aside]


def test_ingest_dir_takes_its_turn_with_the_repos_syncs(tmp_path, monkeypatch):
    """`auryn ingest dir` writes the project subgraph a hook's `auryn sync` writes too (a deep
    re-ingest while a commit hook fires), so it ingests under the same lock -- and, being an
    explicit request, never as the redundant one that steps aside."""
    from auryn.core.project import resolve_project_root

    events = []

    @contextlib.contextmanager
    def recorded(local_dir, *, queue_one=True):
        events.append(("lock", local_dir, queue_one))
        yield True
        events.append("unlock")

    monkeypatch.setattr(sync_lock_mod, "sync_lock", recorded)
    monkeypatch.setattr("auryn.cli._ingest_writer", lambda ctx, graph: None)
    monkeypatch.setattr("auryn.core.ingest.ingest_directory", lambda *a, **k: events.append("ingest"))
    monkeypatch.setattr("auryn.core.ingest.join_deep_phase", lambda: events.append("join"))
    monkeypatch.setattr("auryn.cli._echo_ingest_report", lambda report: None)
    monkeypatch.setattr("auryn.cli._wire_trigger", lambda *a, **k: None)

    res = CliRunner().invoke(cli, ["ingest", "dir", str(tmp_path)])

    assert res.exit_code == 0, res.output
    # "join": a `--deep background` pass still writes the subgraph, so the lock outlasts it
    assert events == [("lock", resolve_project_root(str(tmp_path)), False), "ingest", "join", "unlock"]


@pytest.mark.parametrize("ingest_fails", [False, True])
def test_sync_holds_the_lock_until_a_background_deep_pass_is_done(tmp_path, monkeypatch, ingest_fails):
    """`auryn sync --deep background` gets the fast graph back while a thread still deepens it.
    That thread writes this repo's subgraph too, so the lock is released only once it is joined
    -- also when the ingest itself raised."""
    from auryn.core.graph_client import FalkorDBConfig

    events = []

    @contextlib.contextmanager
    def recorded(local_dir, *, queue_one=True):
        try:
            yield True
        finally:
            events.append("unlock")

    def ingest_directory(*a, **k):
        events.append("ingest")
        if ingest_fails:
            raise RuntimeError("ingest failed")

    monkeypatch.setattr(sync_lock_mod, "sync_lock", recorded)
    monkeypatch.setattr("auryn.cli._resolve_connection", lambda *a, **k: FalkorDBConfig())
    monkeypatch.setattr("auryn.cli.connect", lambda config: None)
    monkeypatch.setattr("auryn.core.ingest.ingest_directory", ingest_directory)
    monkeypatch.setattr("auryn.core.ingest.join_deep_phase", lambda: events.append("join"))
    monkeypatch.setattr("auryn.cli._echo_ingest_report", lambda report: None)

    res = CliRunner().invoke(cli, ["sync", str(tmp_path), "--deep", "background"])

    assert isinstance(res.exception, RuntimeError) if ingest_fails else res.exit_code == 0, res.output
    assert events == ["ingest", "join", "unlock"]
