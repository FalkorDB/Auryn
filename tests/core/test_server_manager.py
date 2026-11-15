"""The lazy local-FalkorDB lifecycle: ensure_server() cold-starts a detached falkordb-lite
daemon on a random loopback port, an ordinary network client (what the MCP server / viewer
use) can talk to it, a second ensure reuses it, and stop_server() tears it down.

Skipped unless falkordb-lite is installed (the optional `embedded` extra; in the dev group).
"""

from __future__ import annotations

import signal

import pytest

pytest.importorskip("redislite.falkordb_client", reason="falkordblite (embedded extra) not installed")

from auryn.core import server_manager as sm
from auryn.core.app_config import Backend


def test_remote_backend_starts_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "cg"))
    cfg = sm.ensure_server(Backend(mode="remote", host="db.example", port=6400, ssl=True))
    assert (cfg.host, cfg.port, cfg.ssl) == ("db.example", 6400, True)
    assert sm._read_server_info() is None  # no local daemon spawned


def test_local_lazy_start_roundtrip_reuse_and_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "cg"))
    monkeypatch.setenv("AURYN_AUTO_INSTALL", "0")  # never network-install mid-test
    try:
        cfg = sm.ensure_server(Backend(mode="local"))
        assert cfg.host == "127.0.0.1" and cfg.port

        info = sm._read_server_info()
        assert info and info["port"] == cfg.port and sm._pid_alive(info["pid"])

        # A separate network client -- exactly what the agent's MCP server and the viewer are --
        # connects to the daemon and round-trips a write.
        from falkordb import FalkorDB

        client = FalkorDB(host=cfg.host, port=cfg.port)
        client.select_graph("t").query("CREATE (:A)")
        assert client.select_graph("t").ro_query("MATCH (n:A) RETURN count(n)").result_set == [[1]]

        # Idempotent: a second ensure reuses the already-running daemon (same port), no new start.
        assert sm.ensure_server(Backend(mode="local")).port == cfg.port
    finally:
        stopped = sm.stop_server()
    assert stopped
    assert sm._read_server_info() is None
    assert not sm._port_open(cfg.port)  # daemon really gone


# ── orphan reaping: a crashed daemon must never leave a second server behind ───────
def test_reap_orphaned_server_kills_listener_on_stale_port(tmp_path, monkeypatch):
    """A prior daemon died uncleanly: its redis-server still listens on the recorded port.
    Reaping must SIGTERM that orphan (and clear the stale rendezvous), not leak it."""
    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "cg"))
    monkeypatch.setattr(sm, "_port_open", lambda port: True)   # orphan still listening
    monkeypatch.setattr(sm, "_pid_alive", lambda pid: True)    # orphan process alive
    monkeypatch.setattr(sm, "_await_all_gone", lambda pids, port, timeout: True)  # dies on SIGTERM
    signalled: list = []
    cleared: list = []
    monkeypatch.setattr(sm, "_signal_pids", lambda pids, sig: signalled.append((list(pids), sig)))
    monkeypatch.setattr(sm, "_clear_server_info", lambda: cleared.append(True))

    sm._reap_orphaned_server({"pid": 111, "server_pid": 222, "port": 6390})

    assert signalled == [([222], signal.SIGTERM)]  # reaped the redis-server pid, SIGTERM only
    assert cleared == [True]


def test_reap_orphaned_server_noop_when_port_free(tmp_path, monkeypatch):
    """Nothing on the recorded port -> the prior server is already gone. Reap must do nothing,
    and in particular never signal a pid the OS may have recycled for an unrelated process."""
    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "cg"))
    monkeypatch.setattr(sm, "_port_open", lambda port: False)  # nothing listening
    monkeypatch.setattr(sm, "_pid_alive", lambda pid: True)    # pid may be reused/unrelated
    signalled: list = []
    monkeypatch.setattr(sm, "_signal_pids", lambda pids, sig: signalled.append((pids, sig)))

    sm._reap_orphaned_server({"pid": 111, "server_pid": 222, "port": 6390})

    assert signalled == []


def test_ensure_server_reaps_orphan_before_cold_start(tmp_path, monkeypatch):
    """With a stale (non-live) rendezvous, a local ensure_server reaps the orphan BEFORE spawning
    a replacement -- so a crashed daemon can never lead to two live servers."""
    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "cg"))
    stale = {"pid": 111, "server_pid": 222, "port": 6390}
    calls: list = []
    monkeypatch.setattr(sm, "local_backend_supported", lambda: True)
    monkeypatch.setattr(sm, "_read_server_info", lambda: stale)
    monkeypatch.setattr(sm, "_live", lambda info: False)  # recorded daemon is dead
    monkeypatch.setattr(sm, "_reap_orphaned_server", lambda info: calls.append(("reap", info)))
    monkeypatch.setattr(sm, "_start_daemon", lambda: (calls.append(("start", None)), {"port": 6401})[1])

    cfg = sm.ensure_server(Backend(mode="local"))

    assert (cfg.host, cfg.port) == ("127.0.0.1", 6401)
    assert calls == [("reap", stale), ("start", None)]  # reaped, and strictly before cold-start
