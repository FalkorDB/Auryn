"""The background deepen daemon: runs the deep (type-resolving) analysis pass *off* the ingest
critical path.

Why a detached daemon rather than an inline pass or a thread (same rationale as the embed daemon):
- **Non-blocking.** The deep pass is heavy (jedi inference, Roslyn builds, rust-analyzer can take
  minutes). Running it inline makes the ingest call (CLI `ingest`, MCP `index_repository`) block until
  it finishes. The fast graph is already queryable the moment phase 1 is done, so there's no reason to
  keep the caller waiting -- `request_deepen` enqueues and returns immediately.
- **Survives the producer exiting.** A one-shot `auryn ingest` process would kill an in-process thread
  the moment it prints its report (`deep="background"` has exactly this flaw). A detached session
  (`start_new_session=True`) outlives it, so the graph still deepens.
- **One worker, many producers.** Several ingests share a single daemon draining a filesystem queue
  (`run/deepen/requests/<graph>.json`).

How "deepen a repo" works without a bespoke code path: the daemon simply **re-runs the normal ingest
with `deep="inline"`**. The fast phase short-circuits when the graph is already at the same revision
with `depth="fast"` (the two-phase depth marker + `_DEPTH_RANK`), so only the deep phase does work and
upgrades the graph to `depth="deep"`. That makes a duplicate or replayed request a cheap no-op (an
already-deep graph short-circuits both phases), and a working tree that moved meanwhile self-heals.

No-orphan guarantees mirror the embed daemon: idle-exit after `IDLE_GRACE`, an fcntl single-flight
run-lock the OS drops on any death, and detached-never-waited spawning (reparent + reap).

CONNECTION. Like the embed daemon, this runs only against the *configured* backend (the shared local
daemon or a configured remote) via `ensure_server(load_backend())` -- ad-hoc connections an external
process couldn't reach (serverless/quickstart embedded DB, explicit `--host`) use `deep="inline"`.
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
import threading
import time
from pathlib import Path

from auryn.core.home import (
    deepen_dir,
    deepen_lock_path,
    deepen_log_path,
    deepen_pid_path,
    deepen_requests_dir,
)

logger = logging.getLogger(__name__)

try:
    import fcntl  # POSIX-only; the local backend already requires it (see server_manager)
except ImportError:  # pragma: no cover - native Windows
    fcntl = None

_POLL_SECONDS = float(os.environ.get("AURYN_DEEPEN_DAEMON_POLL", "") or 2.0)
"""How often the daemon rescans the request queue."""

_IDLE_GRACE_SECONDS = float(os.environ.get("AURYN_DEEPEN_DAEMON_IDLE", "") or 60.0)
"""How long the queue must stay empty before the daemon self-terminates (the no-orphan guard)."""

_MAX_ATTEMPTS = 3
"""Per-repo deepen attempts before a request is dropped -- rides out a transient blip without
spinning forever on a permanently-failing one (e.g. rust-analyzer not installed)."""


# -- pid / liveness (same shape as the embed daemon) ------------------------------------------
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
        return int(deepen_pid_path().read_text().strip())
    except (OSError, ValueError):
        return None


def is_daemon_running() -> bool:
    """Whether a live deepen daemon is already up (a stale pidfile from an unclean death reads as
    not-running, and is reaped on the next spawn)."""
    return _pid_alive(_read_pid())


# -- producer side: enqueue + ensure ----------------------------------------------------------
def _request_path(graph_name: str, project_name: str | None = None) -> Path:
    """A stable, filesystem-safe request filename per (graph, project), so repeated requests for the
    same repo coalesce to one queue entry -- but DISTINCT repos sharing a graph (MCP indexes several
    into one) keep separate entries and both get deepened. blake2b (not the salted builtin hash) so
    DIFFERENT producer processes map the same repo to the same filename (cross-process coalescing)."""
    key = f"{graph_name}\x00{project_name or ''}"
    safe = "".join(c if c.isalnum() or c in "-._" else "_" for c in f"{graph_name}.{project_name or 'repo'}") or "graph"
    safe = safe[:120]  # keep the whole basename (+ `.<10-hex>.json.<pid>.<tid>.tmp`) under the 255-byte FS limit
    digest = hashlib.blake2b(key.encode("utf-8"), digest_size=5).hexdigest()  # the digest, not `safe`, is what disambiguates
    return deepen_requests_dir() / f"{safe}.{digest}.json"


def daemon_supported() -> bool:
    """Whether the background deepen daemon can run on this platform -- it needs POSIX ``fcntl`` for
    the single-flight run-lock. On native Windows it can't, so callers must deepen inline instead."""
    return fcntl is not None


def request_deepen(graph_name: str, local_dir: str, project_name: str | None = None) -> None:
    """Enqueue a background deep pass for `graph_name` and make sure a daemon is running to drain it.
    Returns immediately -- the deep pass happens in the daemon, which re-runs the ingest of `local_dir`
    with deep="inline" (the fast phase short-circuits). Coalescing: a second request for the same graph
    overwrites the first, so a burst of resyncs doesn't pile up queue entries."""
    requests = deepen_requests_dir()
    requests.mkdir(parents=True, exist_ok=True)
    target = _request_path(graph_name, project_name)
    # per-process AND per-thread temp name -> no two concurrent writers clobber each other's
    # half-written file before the atomic rename publishes it.
    tmp = target.with_name(f"{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    payload: dict[str, str] = {"graph": graph_name, "local_dir": local_dir}
    if project_name:
        payload["project_name"] = project_name
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, target)  # atomic publish: the daemon never reads a half-written request
    ensure_deepen_daemon()


def ensure_deepen_daemon() -> bool:
    """Start the detached deepen daemon if one isn't already up. Returns True if it's running (or was
    just spawned). A running daemon picks up newly-queued requests on its next poll."""
    if fcntl is None:  # pragma: no cover - native Windows has no local daemon anyway
        logger.debug("Deepen daemon unsupported on this platform; skipping background deep pass")
        return False
    if is_daemon_running():
        return True
    deepen_dir().mkdir(parents=True, exist_ok=True)
    # Open in a `with` so the PARENT always releases its copy of the fd (even if Popen raises); the
    # detached child keeps its own dup'd fd for stdout/stderr.
    with open(deepen_log_path(), "ab") as log:
        subprocess.Popen(
            [sys.executable, "-m", "auryn", "deepen-daemon"],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True,
        )
    logger.info("Spawned background deepen daemon")
    return True


def stop_deepen_daemon() -> bool:
    """Signal a running deepen daemon to stop (it also idle-exits on its own). Returns False if nothing
    was running. Mainly for tests and a clean teardown."""
    pid = _read_pid()
    if not _pid_alive(pid):
        with contextlib.suppress(OSError):
            deepen_pid_path().unlink()
        return False
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and _pid_alive(pid):
        time.sleep(0.05)
    with contextlib.suppress(OSError):
        deepen_pid_path().unlink()
    return True


# -- daemon side ------------------------------------------------------------------------------
def run_deepen_daemon() -> None:
    """The daemon body (foreground; `auryn deepen-daemon` runs this in a detached session). Holds the
    single-flight run-lock, drains the request queue running each repo's deep pass, and self-terminates
    once the queue has been idle past the grace window. Safe to invoke when another daemon already holds
    the lock -- it returns immediately."""
    if fcntl is None:  # pragma: no cover - native Windows
        logger.warning("Deepen daemon unsupported on this platform")
        return
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    deepen_dir().mkdir(parents=True, exist_ok=True)
    lock_file = open(deepen_lock_path(), "w")  # noqa: SIM115 -- held open for the daemon's lifetime
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        logger.info("Another deepen daemon already holds the lock; exiting")
        lock_file.close()
        return

    stop = _install_signal_handlers()
    deepen_pid_path().write_text(str(os.getpid()))
    try:
        _drain_loop(stop)
    finally:
        with contextlib.suppress(OSError):
            deepen_pid_path().unlink()
        with contextlib.suppress(Exception):
            fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()
        logger.info("Deepen daemon exiting")


def _install_signal_handlers() -> dict:
    """A tiny mutable flag the SIGTERM/SIGINT handlers flip so the drain loop exits cleanly."""
    stop = {"flag": False}
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(ValueError):  # only valid on the main thread
            signal.signal(sig, lambda *_: stop.__setitem__("flag", True))
    return stop


def _recover_inflight() -> None:
    """Reclaim requests a previous daemon crashed mid-deepen on. `_process_request` claims a request by
    renaming `<g>.json` -> `<g>.inflight`; if the daemon then dies, the drain loop (which scans only
    `*.json`) would never see it again. Renaming every stray `*.inflight` back re-queues it (the deep
    pass is idempotent -- an already-deep graph short-circuits, so a half-done one simply finishes)."""
    for inflight in deepen_requests_dir().glob("*.inflight"):
        with contextlib.suppress(OSError):
            os.replace(inflight, inflight.with_suffix(".json"))


def _drain_loop(stop: dict) -> None:
    _recover_inflight()  # re-queue anything a prior daemon crashed on before we start draining
    attempts: dict[str, int] = {}
    client = None  # resolved lazily; cached across requests, dropped on a connection error
    idle_since: float | None = None

    logger.info("Deepen daemon up (poll=%.1fs idle-grace=%.1fs)", _POLL_SECONDS, _IDLE_GRACE_SECONDS)
    while not stop["flag"]:
        requests = sorted(deepen_requests_dir().glob("*.json"))
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
            client = _process_request(req, client, attempts)
        time.sleep(_POLL_SECONDS)  # breathe between passes so a failing+requeued request can't hot-spin


def _process_request(req: Path, client, attempts: dict[str, int]):
    """Deepen one queued repo. Claims the request by renaming it to `.inflight` (so a second pass -- or
    another daemon mid-handover -- won't double-take it), runs the deep pass, then deletes the claim on
    success. On failure it re-queues with a bounded retry; after `_MAX_ATTEMPTS` it drops the request
    (the graph stays at depth=fast; a future ingest re-requests it). Returns the (possibly refreshed)
    client."""
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
        local_dir = data["local_dir"]
        project_name = data.get("project_name")
        if not isinstance(graph_name, str) or not graph_name or not isinstance(local_dir, str) or not local_dir:
            raise ValueError("request missing graph or local_dir")
    except (OSError, ValueError, KeyError, TypeError):
        # Any shape of corrupt entry is dropped, never propagated -- one bad file must not take the
        # daemon down.
        logger.warning("Dropping malformed deepen request %s", inflight.name)
        inflight.unlink(missing_ok=True)
        return client

    key = req.name  # retry accounting per (graph, project) request, not per graph (repos share graphs)
    try:
        client = client or _connect()
        _deepen(client, graph_name, local_dir, project_name)
        inflight.unlink(missing_ok=True)
        attempts.pop(key, None)
    except Exception:
        attempts[key] = attempts.get(key, 0) + 1
        n = attempts[key]
        if n < _MAX_ATTEMPTS:
            logger.exception("Deepening %r failed (attempt %d/%d); will retry", key, n, _MAX_ATTEMPTS)
            # Re-queue for the next pass -- but only if a NEWER request for the same (graph, project)
            # hasn't been published meanwhile (a replacement checkout): don't clobber it with this stale
            # one. If one has, drop this inflight; the newer `.json` supersedes it.
            if req.exists():
                inflight.unlink(missing_ok=True)
            else:
                with contextlib.suppress(OSError):
                    os.replace(inflight, req)
        else:
            logger.exception("Deepening %r failed %d times; dropping request", key, n)
            inflight.unlink(missing_ok=True)
            attempts.pop(key, None)
        client = None  # a DB error may have poisoned the client -- re-resolve next time
    return client


def _deepen(client, graph_name: str, local_dir: str, project_name: str | None) -> None:
    """Run the deep pass for one repo by re-running its ingest with deep="inline" against the already
    fast-built graph -- the fast phase short-circuits (same sha + depth=fast), so only the deep upgrade
    does work. Held under the per-repo `sync_lock` (as the CLI/MCP/hook syncs are), so a concurrent
    resync can't interleave with the delta read+write. Raises on a deep-phase error so the caller
    retries (ingest_directory reports an error on the LanguageReport rather than raising)."""
    from auryn.core.graph_client import GraphWriter
    from auryn.core.ingest import ingest_directory
    from auryn.core.project import resolve_project_root
    from auryn.core.sync_lock import sync_lock

    if not Path(local_dir).is_dir():
        logger.warning("Deepen skipped for %r: local dir %s no longer exists", graph_name, local_dir)
        return
    writer = GraphWriter(client.select_graph(graph_name))
    # The daemon reconnects to the CONFIGURED backend (_connect). If the producer ingested against a
    # DIFFERENT backend (a library caller's custom server; the embedded-DB case is already filtered out
    # before enqueue), the configured backend has no such project -- re-ingesting here would wrongly
    # build a full graph on the wrong server. So only deepen a project the fast pass already put here;
    # a mismatch (or a since-deleted graph) is a safe no-op.
    if project_name and not writer.ro_query(
        "MATCH (p:Project {name: $n}) RETURN 1 LIMIT 1", {"n": project_name}
    ).result_set:
        logger.warning("Deepen skipped for %r/%r: no matching project on the configured backend "
                       "(stale request, deleted graph, or a non-configured backend)", graph_name, project_name)
        return
    with sync_lock(resolve_project_root(local_dir), queue_one=False):
        # embed="off": the PRODUCER's ingest already made the embedding decision (and deep doesn't change
        # embeddable node content -- name/doc/source -- only CALLS edges + unresolved_calls), so the
        # daemon must not re-embed, which also honors an original embed="off".
        report = ingest_directory(writer, local_dir, project_name=project_name, deep="inline", embed="off")
    errored = [r.language for r in report.languages if r.deep_status == "error"]
    if errored:  # surface as a failure so _process_request's bounded retry kicks in
        raise RuntimeError(f"deep pass errored for {graph_name!r}: {', '.join(errored)}")
    deepened = sum(1 for r in report.languages if r.deep_status == "inline")
    logger.info("Deepened graph %r (%s): %d language(s) upgraded", graph_name, local_dir, deepened)


def _connect():
    """A client to the *configured* backend -- the same one every producer that enqueues a background
    deepen used (ad-hoc connections force inline and never reach here)."""
    from auryn.core.app_config import load_backend
    from auryn.core.graph_client import connect_client
    from auryn.core.server_manager import ensure_server

    return connect_client(ensure_server(load_backend()))
