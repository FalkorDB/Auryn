"""The background embedding daemon: builds a graph's vectors *off* the ingest critical path.

Why a detached daemon rather than an inline pass or a thread:
- **Non-blocking.** Embedding is heavy ONNX compute over every callable/type/file. Running it
  inline made the ingest call (CLI `ingest`, MCP `index_repository`) block until the last vector
  was indexed. The graph is already built and queryable before embedding starts, so there's no
  reason to keep the caller waiting -- `request_embed` enqueues and returns immediately.
- **Survives the producer exiting.** A one-shot `auryn ingest` process would kill an in-process
  thread the moment it prints its report. A detached session (`start_new_session=True`) outlives
  it, so the vectors still get built.
- **One worker, many producers.** Several ingests (different repos, resyncs) share a single
  daemon: the model (~160M params) loads once and is reused across every graph. The shared seam
  is a filesystem queue (`run/embed/requests/<graph>.json`); producers drop requests, the daemon
  drains them. Idempotent by construction -- `embed_graph` only touches nodes without a vector, so
  a duplicate or replayed request is a cheap no-op.

No-orphan guarantees (the daemon must never linger):
1. **Idle-exit.** When the queue stays empty for `IDLE_GRACE` seconds, the daemon terminates. It
   only exists while there is work.
2. **flock run-lock.** Held for the daemon's whole life, so a second daemon can't start (and the
   OS drops the lock automatically on any death -- even a crash -- so single-flight self-heals).
3. **Reparent + reap.** Spawned detached and never `wait()`-ed, so on the producer's exit it
   reparents to init and is reaped -- never a zombie.

CONNECTION. Background embedding only runs against the *configured* backend (the shared local
daemon or a configured remote), so the daemon reconnects independently via
`ensure_server(load_backend())` -- the request needs only a graph name. Ad-hoc connections an
external process couldn't reach (the serverless/quickstart embedded DB, explicit `--host` flags)
use `embed="inline"` instead and never reach here.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from auryn.core.home import (
    embed_dir,
    embed_lock_path,
    embed_log_path,
    embed_pid_path,
    embed_requests_dir,
    run_dir,
)

logger = logging.getLogger(__name__)

try:
    import fcntl  # POSIX-only; the local backend already requires it (see server_manager)
except ImportError:  # pragma: no cover - native Windows
    fcntl = None

_POLL_SECONDS = float(os.environ.get("AURYN_EMBED_DAEMON_POLL", "") or 2.0)
"""How often the daemon rescans the request queue."""

_IDLE_GRACE_SECONDS = float(os.environ.get("AURYN_EMBED_DAEMON_IDLE", "") or 60.0)
"""How long the queue must stay empty before the daemon self-terminates (the no-orphan guard)."""

_MAX_ATTEMPTS = 3
"""Per-graph embed attempts before a request is dropped -- rides out a transient DB blip without
spinning forever on a permanently-failing one (e.g. the backend went away)."""


# ── pid / liveness (same shape as watcher/server_manager) ───────────────────────────────────
def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_pid() -> int | None:
    try:
        return int(embed_pid_path().read_text().strip())
    except (OSError, ValueError):
        return None


def is_daemon_running() -> bool:
    """Whether a live embed daemon is already up (a stale pidfile from an unclean death reads as
    not-running, and is reaped on the next spawn)."""
    return _pid_alive(_read_pid())


# ── producer side: enqueue + ensure ─────────────────────────────────────────────────────────
def _request_path(graph_name: str) -> Path:
    """A stable, filesystem-safe request filename per graph, so repeated requests for the same
    graph coalesce to one queue entry (the real name is stored inside the file)."""
    safe = "".join(c if c.isalnum() or c in "-._" else "_" for c in graph_name) or "graph"
    # A short digest disambiguates names that sanitise to the same thing. hashlib (not the builtin
    # hash(), which is per-process salted) so DIFFERENT producer processes map the same graph to the
    # same filename -- that's what makes cross-process coalescing work.
    digest = hashlib.blake2b(graph_name.encode("utf-8"), digest_size=4).hexdigest()
    return embed_requests_dir() / f"{safe}.{digest}.json"


def daemon_supported() -> bool:
    """Whether the background embed daemon can run on this platform -- it needs POSIX ``fcntl`` for
    the single-flight run-lock. On native Windows it can't, so callers must embed inline instead of
    queueing a request nothing would ever drain."""
    return fcntl is not None


def request_embed(graph_name: str, model: str | None = None) -> None:
    """Enqueue a background embed for `graph_name` and make sure a daemon is running to drain it.
    Returns immediately -- the heavy work happens in the daemon. `model` records the embedding model
    this ingest intended, so the daemon embeds the graph with it rather than whatever model the
    daemon's own environment resolves to (a graph's vector index is fixed-dimension -- mixing models
    in one graph is incoherent). Coalescing: a second request for the same graph overwrites the
    first, so a burst of resyncs doesn't pile up queue entries."""
    requests = embed_requests_dir()
    requests.mkdir(parents=True, exist_ok=True)
    target = _request_path(graph_name)
    # A per-process temp name so concurrent producers (different processes, or threads) never
    # scribble over each other's half-written file before the atomic rename publishes it.
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    payload: dict[str, str] = {"graph": graph_name}
    if model:
        payload["model"] = model
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, target)  # atomic publish: the daemon never reads a half-written request
    ensure_embed_daemon()


def ensure_embed_daemon() -> bool:
    """Start the detached embed daemon if one isn't already up. Returns True if it's running (or
    was just spawned). A running daemon picks up newly-queued requests on its next poll, so this is
    a cheap no-op in the common case."""
    if fcntl is None:  # pragma: no cover - native Windows has no local daemon anyway
        logger.debug("Embed daemon unsupported on this platform; skipping background embed")
        return False
    if is_daemon_running():
        return True
    embed_dir().mkdir(parents=True, exist_ok=True)
    log = open(embed_log_path(), "ab")  # noqa: SIM115 -- handed to the detached child
    subprocess.Popen(
        [sys.executable, "-m", "auryn", "embed-daemon"],
        stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True,
    )
    logger.info("Spawned background embed daemon")
    return True


def stop_embed_daemon() -> bool:
    """Signal a running embed daemon to stop (it also idle-exits on its own). Returns False if
    nothing was running. Mainly for tests and a clean `auryn` teardown."""
    pid = _read_pid()
    if not _pid_alive(pid):
        with contextlib.suppress(OSError):
            embed_pid_path().unlink()
        return False
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and _pid_alive(pid):
        time.sleep(0.05)
    with contextlib.suppress(OSError):
        embed_pid_path().unlink()
    return True


# ── daemon side ─────────────────────────────────────────────────────────────────────────────
def run_embed_daemon() -> None:
    """The daemon body (foreground; `auryn embed-daemon` runs this in a detached session). Holds
    the single-flight run-lock, drains the request queue embedding each graph, and self-terminates
    once the queue has been idle past the grace window. Safe to invoke when another daemon already
    holds the lock -- it returns immediately."""
    if fcntl is None:  # pragma: no cover - native Windows
        logger.warning("Embed daemon unsupported on this platform")
        return
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    embed_dir().mkdir(parents=True, exist_ok=True)
    lock_file = open(embed_lock_path(), "w")  # noqa: SIM115 -- held open for the daemon's lifetime
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        logger.info("Another embed daemon already holds the lock; exiting")
        lock_file.close()
        return

    stop = _install_signal_handlers()
    embed_pid_path().write_text(str(os.getpid()))
    try:
        _drain_loop(stop)
    finally:
        with contextlib.suppress(OSError):
            embed_pid_path().unlink()
        with contextlib.suppress(Exception):
            fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()
        logger.info("Embed daemon exiting")


def _install_signal_handlers() -> dict:
    """A tiny mutable flag the SIGTERM/SIGINT handlers flip so the drain loop exits cleanly."""
    stop = {"flag": False}
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(ValueError):  # only valid on the main thread
            signal.signal(sig, lambda *_: stop.__setitem__("flag", True))
    return stop


def _recover_inflight() -> None:
    """Reclaim requests a previous daemon crashed mid-embed on. `_process_request` claims a request
    by renaming `<g>.json` -> `<g>.inflight`; if the daemon then dies, the drain loop (which scans
    only `*.json`) would never see it again and that graph's vectors would be stranded. Renaming
    every stray `*.inflight` back to `*.json` at startup re-queues it (embedding is idempotent, so a
    half-done one simply finishes)."""
    for inflight in embed_requests_dir().glob("*.inflight"):
        with contextlib.suppress(OSError):
            os.replace(inflight, inflight.with_suffix(".json"))


def _drain_loop(stop: dict) -> None:
    from auryn.core.embeddings import get_embedder

    _recover_inflight()  # re-queue anything a prior daemon crashed on before we start draining
    # One embedder per model id, loaded on first use and reused -- requests carry their model so a
    # custom-model graph is embedded with its own model, not the daemon's ambient one.
    embedders: dict[str | None, object] = {}

    def embedder_for(model: str | None):
        if model not in embedders:
            embedders[model] = get_embedder(model)
        return embedders[model]

    attempts: dict[str, int] = {}
    client = None  # resolved lazily; cached across requests, dropped on a connection error
    idle_since: float | None = None

    logger.info("Embed daemon up (poll=%.1fs idle-grace=%.1fs)", _POLL_SECONDS, _IDLE_GRACE_SECONDS)
    while not stop["flag"]:
        requests = sorted(embed_requests_dir().glob("*.json"))
        if not requests:
            if idle_since is None:
                idle_since = time.monotonic()
            elif time.monotonic() - idle_since >= _IDLE_GRACE_SECONDS:
                return  # queue idle past the grace window -> self-terminate (no orphan)
            time.sleep(_POLL_SECONDS)
            continue

        idle_since = None
        for req in requests:
            if stop["flag"]:
                return
            client = _process_request(req, embedder_for, client, attempts)
        time.sleep(_POLL_SECONDS)  # breathe between passes so a failing+requeued request can't hot-spin


def _process_request(req: Path, embedder_for, client, attempts: dict[str, int]):
    """Embed one queued graph. Claims the request by renaming it to `.inflight` (so a second pass
    -- or another daemon mid-handover -- won't double-take it), embeds, then deletes the claim on
    success. On failure it re-queues with a bounded retry; after `_MAX_ATTEMPTS` it drops the
    request (the nodes stay pending and a future ingest re-requests them). `embedder_for` maps a
    request's model id to the (cached) embedder to use. Returns the (possibly refreshed) client."""
    inflight = req.with_suffix(".inflight")
    try:
        os.replace(req, inflight)
    except FileNotFoundError:
        return client  # already claimed/removed this cycle -- nothing to do

    try:
        data = json.loads(inflight.read_text())
        if not isinstance(data, dict):
            raise ValueError("request payload is not a JSON object")
        graph_name = data["graph"]
        model = data.get("model")
        if not isinstance(graph_name, str) or not graph_name:
            raise ValueError("request has no graph name")
    except (OSError, ValueError, KeyError, TypeError):
        # Any shape of corrupt entry is dropped, never propagated -- one bad file must not take the
        # daemon down (e.g. a JSON array makes `data["graph"]` raise TypeError).
        logger.warning("Dropping malformed embed request %s", inflight.name)
        inflight.unlink(missing_ok=True)
        return client

    try:
        client = client or _connect()
        from auryn.core.embeddings.worker import embed_graph
        from auryn.core.graph_client import GraphWriter

        writer = GraphWriter(client.select_graph(graph_name))
        stats = embed_graph(writer, embedder_for(model))
        logger.info("Embedded graph %r: %d embedded, %d scanned", graph_name, stats.embedded, stats.scanned)
        inflight.unlink(missing_ok=True)
        attempts.pop(graph_name, None)
    except Exception:
        attempts[graph_name] = attempts.get(graph_name, 0) + 1
        n = attempts[graph_name]
        if n < _MAX_ATTEMPTS:
            logger.exception("Embedding graph %r failed (attempt %d/%d); will retry", graph_name, n, _MAX_ATTEMPTS)
            with contextlib.suppress(OSError):
                os.replace(inflight, req)  # re-queue for the next pass
        else:
            logger.exception("Embedding graph %r failed %d times; dropping request", graph_name, n)
            inflight.unlink(missing_ok=True)
            attempts.pop(graph_name, None)
        client = None  # a DB error may have poisoned the client -- re-resolve next time
    return client


def _connect():
    """A client to the *configured* backend -- the same one every producer that enqueues a
    background embed used (ad-hoc connections force inline and never reach here)."""
    from auryn.core.app_config import load_backend
    from auryn.core.graph_client import connect_client
    from auryn.core.server_manager import ensure_server

    return connect_client(ensure_server(load_backend()))
