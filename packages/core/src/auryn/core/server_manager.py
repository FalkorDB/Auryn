"""Lazy lifecycle for the local (falkordb-lite) FalkorDB backend.

The local backend is a *shared, detached daemon*, not an in-process DB: falkordb-lite's server
dies with the process that created it, and the agent's MCP server, the viewer, and the CLI are
separate processes that must see the same graphs. So exactly one daemon holds the bundled
`redis-server`, on a random loopback port, and everyone else connects to it as an ordinary
network client.

:func:`ensure_server` is the one entry point callers use: for ``remote`` it just returns the
configured connection; for ``local`` it returns the running daemon's connection, cold-starting
it (once, under a lock) if needed. Nothing is always-on -- a fresh boot's first use pays a ~1s
cold start, then every later use reconnects to the already-running daemon.

See docs/adr/0001-onboarding-and-server-lifecycle.md.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # native Windows -- the local backend is unsupported there (see below), so
    fcntl = None  # this module still imports and ensure_server()/run_server() raise a clean error.

from auryn.core.app_config import Backend, load_backend
from auryn.core.graph_client import FalkorDBConfig
from auryn.core.home import (
    home_dir,
    run_dir,
    serve_log_path,
    server_info_path,
    server_lock_path,
    store_db_path,
)
from auryn.core.platform_support import LOCAL_UNSUPPORTED_MESSAGE, local_backend_supported

logger = logging.getLogger("auryn.server")

_STARTUP_TIMEOUT_S = 30.0  # generous; a cold falkordb-lite start is ~1s
_CONNECT_TIMEOUT_S = 0.5
_LOOPBACK = "127.0.0.1"


# ── discovery / liveness ─────────────────────────────────────────────────────
def _read_server_info() -> dict | None:
    """The daemon's ``run/server.json`` (pid, port, ...), or None if absent/unreadable."""
    try:
        return json.loads(server_info_path().read_text())
    except (OSError, ValueError):
        return None


def _write_server_info(port: int, db: Path, server_pid: int | None) -> None:
    run_dir().mkdir(parents=True, exist_ok=True)
    server_info_path().write_text(
        json.dumps(
            {
                "pid": os.getpid(),  # the `serve` daemon
                "server_pid": server_pid,  # the bundled redis-server it manages
                "port": port,
                "db": str(db),
                "started": time.time(),
            }
        )
    )


def _clear_server_info() -> None:
    with contextlib.suppress(OSError):
        server_info_path().unlink()


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else -- treat as alive
    return True


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(_CONNECT_TIMEOUT_S)
        return s.connect_ex((_LOOPBACK, port)) == 0


def _live(info: dict | None) -> bool:
    """A recorded daemon is usable only if its pid is alive *and* its port accepts a
    connection -- guards against a stale file after a crash/reboot."""
    return bool(info) and _pid_alive(info.get("pid")) and _port_open(int(info["port"]))


def _free_port() -> int:
    """A currently-free loopback TCP port. A tiny TOCTOU window exists between here and the
    daemon binding it; on the rare collision the daemon exits early and the caller retries."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((_LOOPBACK, 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def _startup_lock():
    """Serialize cold-starts so two callers racing on a down server don't both spawn a daemon."""
    run_dir().mkdir(parents=True, exist_ok=True)
    with open(server_lock_path(), "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


# ── public API ───────────────────────────────────────────────────────────────
def ensure_server(backend: Backend | None = None) -> FalkorDBConfig:
    """Return a FalkorDBConfig ready to :func:`connect_client`. ``remote`` returns the
    configured connection unchanged (nothing to start). ``local`` returns the running
    daemon's loopback connection, cold-starting the daemon under a lock if it isn't up."""
    backend = backend or load_backend()
    if backend.mode == "remote":
        return FalkorDBConfig(
            host=backend.host or "localhost",
            port=backend.port or 6379,
            username=backend.username,
            password=backend.password,
            ssl=backend.ssl,
        )

    if not local_backend_supported():  # native Windows: no POSIX redis-server / no fcntl
        raise RuntimeError(LOCAL_UNSUPPORTED_MESSAGE)

    info = _read_server_info()
    if _live(info):
        return FalkorDBConfig(host=_LOOPBACK, port=int(info["port"]))
    with _startup_lock():
        info = _read_server_info()  # re-check: another caller may have just started it
        if _live(info):
            return FalkorDBConfig(host=_LOOPBACK, port=int(info["port"]))
        _reap_orphaned_server(info)  # a redis-server left by a prior unclean shutdown, if any
        info = _start_daemon()
    return FalkorDBConfig(host=_LOOPBACK, port=int(info["port"]))


def stop_server() -> bool:
    """Stop the local daemon and its bundled redis-server if running, and clear the rendezvous
    file. Returns False if nothing was running. No-op for a remote backend.

    Kills BOTH tracked pids -- the `serve` daemon and the redis-server it manages -- directly,
    SIGTERM first (redis saves its RDB on SIGTERM), SIGKILL only as a fallback. redislite runs
    the server outside the daemon's process group and its atexit cleanup can't be relied on for
    a signalled process, so a group-kill or atexit would leak the server; the tracked pid is
    authoritative. Waits until both are gone and the port is released before returning."""
    info = _read_server_info()
    if not info:
        return False
    pids = [p for p in (info.get("pid"), info.get("server_pid")) if p]
    port = info.get("port")
    if not any(_pid_alive(p) for p in pids):
        _clear_server_info()
        return False
    _signal_pids(pids, signal.SIGTERM)
    if not _await_all_gone(pids, port, timeout=5.0):
        _signal_pids(pids, signal.SIGKILL)  # last resort
        _await_all_gone(pids, port, timeout=2.0)
    _clear_server_info()
    return True


def _signal_pids(pids: list[int], sig: int) -> None:
    for pid in pids:
        if _pid_alive(pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, sig)


def _await_all_gone(pids: list[int], port: int | None, timeout: float) -> bool:
    """True once every pid is gone *and* the port is released (redis-server can outlive its
    SIGTERM briefly while it saves and closes the listening socket)."""
    deadline = time.time() + timeout
    while True:
        gone = not any(_pid_alive(p) for p in pids) and (not port or not _port_open(int(port)))
        if gone or time.time() >= deadline:
            return gone
        time.sleep(0.1)


def run_server(port: int | None = None, db_path: str | None = None) -> None:
    """The daemon body (``auryn serve``): start the bundled falkordb-lite server on a
    (random, loopback-only) port, publish the rendezvous file, and block until terminated.
    Normally spawned detached by :func:`ensure_server`; runnable directly to inspect/debug."""
    if not local_backend_supported():  # native Windows: no POSIX redis-server / no fcntl
        raise RuntimeError(LOCAL_UNSUPPORTED_MESSAGE)
    port = port or _free_port()
    db = Path(db_path) if db_path else store_db_path()
    db.parent.mkdir(parents=True, exist_ok=True)

    # falkordb-lite is the optional `embedded` extra; auto-install on demand (opt-out), same
    # policy as the live-DB drivers -- a remote-only machine never needs it.
    from auryn.core.plugins import ensure_importable

    module = ensure_importable("redislite.falkordb_client", "falkordblite>=0.10")
    server = module.FalkorDB(str(db), serverconfig={"port": str(port), "bind": _LOOPBACK})
    server.list_graphs()  # force the round-trip that confirms the server is actually up
    server_pid = getattr(getattr(server, "connection", None), "pid", None)  # the redis-server pid

    _write_server_info(port, db, server_pid)
    logger.info("local FalkorDB (falkordb-lite) listening on %s:%s  db=%s", _LOOPBACK, port, db)

    # Block until asked to stop. sigwait (with the signals masked) is race-free -- unlike a
    # handler + signal.pause(), a SIGTERM that arrives "between" checks can't be missed.
    stop_signals = {signal.SIGTERM, signal.SIGINT}
    signal.pthread_sigmask(signal.SIG_BLOCK, stop_signals)
    try:
        signal.sigwait(list(stop_signals))
    finally:
        # Explicitly stop the bundled redis-server (SIGTERM -> it saves its RDB, then exits).
        # redislite runs it outside our process group and its atexit doesn't reliably fire for
        # a signalled daemon, so killing the tracked pid -- not atexit or a group kill -- is what
        # actually prevents an orphaned server.
        if server_pid:
            with contextlib.suppress(ProcessLookupError):
                os.kill(server_pid, signal.SIGTERM)
        _clear_server_info()


# ── internals ────────────────────────────────────────────────────────────────
def _reap_orphaned_server(info: dict | None) -> None:
    """Kill a bundled redis-server orphaned by a daemon that died WITHOUT running its shutdown
    (crash / SIGKILL / reboot). redislite runs the server outside the daemon's process group, so
    an unclean death leaves it listening on the recorded port while the rendezvous goes stale --
    and the next cold-start would otherwise spawn a *second* server, letting instances pile up
    over a machine's lifetime. Called under the startup lock, only once the recorded daemon is
    known NOT live, just before spawning a replacement -- so there is always at most one server.

    Guarded by the recorded port still being open: a bare live pid is not enough to act on (the
    OS may have recycled it for an unrelated process), but something still listening on the exact
    recorded loopback port is confidently our leaked server, so it is safe to signal."""
    if not info:
        return
    server_pid = info.get("server_pid")
    port = info.get("port")
    if not (port and _port_open(int(port)) and _pid_alive(server_pid)):
        return  # nothing listening on the recorded port -> no orphan to reap
    logger.warning(
        "reaping orphaned local FalkorDB server (pid=%s, port=%s) left by a prior unclean shutdown",
        server_pid, port,
    )
    _signal_pids([server_pid], signal.SIGTERM)  # SIGTERM -> redis saves its RDB, then exits
    if not _await_all_gone([server_pid], port, timeout=5.0):
        _signal_pids([server_pid], signal.SIGKILL)  # last resort
        _await_all_gone([server_pid], port, timeout=2.0)
    _clear_server_info()


def _start_daemon() -> dict:
    """Spawn ``auryn serve`` fully detached (its own session; stdio -> serve.log so it
    never touches a caller's stdin/stdout) and wait until it publishes a live rendezvous."""
    run_dir().mkdir(parents=True, exist_ok=True)
    log = open(serve_log_path(), "ab")  # noqa: SIM115 -- handed to the child; closed on our exit
    proc = subprocess.Popen(
        [sys.executable, "-m", "auryn", "serve"],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        start_new_session=True,  # detach: survives this process, own process group
        close_fds=True,
        cwd=str(home_dir()),
    )
    deadline = time.time() + _STARTUP_TIMEOUT_S
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"local FalkorDB daemon exited early (code {proc.returncode}); see {serve_log_path()}"
            )
        info = _read_server_info()
        if info and info.get("pid") == proc.pid and _port_open(int(info["port"])):
            return info
        time.sleep(0.1)
    raise RuntimeError(f"timed out starting local FalkorDB; see {serve_log_path()}")
