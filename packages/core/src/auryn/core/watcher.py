"""Save-trigger watcher daemon (ADR 0003, `save` trigger).

One detached process watches every save-triggered repo's working directory (from the
`[[repo]]` registry), and on a code-file change fires an incremental `auryn sync` for that
repo -- debounced (coalesce a burst of saves into one resync), routed (a changed path -> its
owning repo by longest registered-root prefix), and non-overlapping (skip if that repo's sync
is still running). It reconciles its watch-set with the registry periodically, and exits when
no save-triggered repos remain. Detached lifecycle (pid/log under `run/`) mirrors
server_manager's local FalkorDB daemon.
"""

from __future__ import annotations

import contextlib
import fnmatch
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from auryn.core.app_config import effective_trigger, load_config
from auryn.core.home import run_dir, watcher_log_path, watcher_pid_path

logger = logging.getLogger("auryn.watcher")

_DEBOUNCE_S = 0.4
_RECONCILE_S = 5.0
_BASE_EXCLUDED = {".git", "node_modules", "__pycache__", ".venv", "venv", "env",
                  "dist", "build", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox"}


# ── what to watch / react to (from the plugin + repo registries) ─────────────────────────
def _watch_patterns() -> tuple[str, ...]:
    """Basename globs of every registered language (union of their `match_patterns`), so the watcher
    reacts to a changed file by its BASENAME -- catching an extensionless `Dockerfile` (empty
    `Path.suffix`) and basename variants (`Dockerfile.prod`, `Chart.yaml`) a suffix set could not."""
    from auryn.core.registry import discover_git_languages

    out: dict[str, None] = {}
    for lang in discover_git_languages().values():
        out.update(dict.fromkeys(lang.match_patterns()))
    return tuple(out)


def _excluded_dirs() -> set[str]:
    from auryn.core.registry import discover_git_languages

    out = set(_BASE_EXCLUDED)
    for lang in discover_git_languages().values():
        out.update(lang.excluded_dir_names)
    return out


def _save_repo_dirs() -> set[str]:
    """Resolved working dirs of every registered repo whose effective trigger is `save` and
    that still exists on disk."""
    dirs: set[str] = set()
    for reg in load_config().repos:
        if effective_trigger(reg) == "save":
            p = Path(reg.local_dir)
            if p.is_dir():
                dirs.add(str(p.resolve()))
    return dirs


def _owning_repo(path: str, roots) -> str | None:
    """The longest registered root that is a prefix of `path` (so a change routes to exactly
    one repo, even with nested roots). Both sides are symlink-resolved so a watchdog event path
    (`/var/...` on macOS) matches a resolved watch root (`/private/var/...`)."""
    rp = str(Path(path).resolve())
    best = None
    for root in roots:
        if rp == root or rp.startswith(root + os.sep):
            if best is None or len(root) > len(best):
                best = root
    return best


# ── the watcher loop ─────────────────────────────────────────────────────────────────────
def run_watcher(stop_event: threading.Event | None = None,
                debounce: float = _DEBOUNCE_S, reconcile_every: float = _RECONCILE_S) -> None:
    """Run the watcher in THIS process until stopped (SIGTERM/SIGINT, or `stop_event` when one
    is supplied -- tests pass their own; the daemon lets None install signal handlers). Owns the
    pid file for its lifetime. Returns immediately if nothing is save-triggered."""
    from auryn.core.plugins import ensure_importable

    ensure_importable("watchdog.observers", "watchdog>=4")
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    patterns = _watch_patterns()
    excluded = _excluded_dirs()
    observers: dict[str, object] = {}       # repo_dir -> Observer
    dirty: dict[str, float] = {}            # repo_dir -> last relevant-event monotonic time
    running: dict[str, subprocess.Popen] = {}
    lock = threading.Lock()

    def note_change(path: str) -> None:
        p = Path(path)
        if excluded & set(p.parts) or not any(fnmatch.fnmatchcase(p.name, pat) for pat in patterns):
            return
        repo = _owning_repo(path, observers.keys())
        if repo is not None:
            with lock:
                dirty[repo] = time.monotonic()

    class _Handler(FileSystemEventHandler):
        def on_any_event(self, event) -> None:
            if getattr(event, "is_directory", False):
                return
            note_change(event.src_path)
            dest = getattr(event, "dest_path", None)
            if dest:
                note_change(dest)

    handler = _Handler()

    def reconcile() -> set[str]:
        want = _save_repo_dirs()
        for d in want - set(observers):
            obs = Observer()
            obs.schedule(handler, d, recursive=True)
            obs.start()
            observers[d] = obs
            logger.info("watching %s", d)
        for d in set(observers) - want:
            with contextlib.suppress(Exception):
                observers.pop(d).stop()
        return want

    def spawn_sync(repo: str) -> None:
        proc = running.get(repo)
        if proc is not None and proc.poll() is None:  # a sync is still in flight -> re-queue
            with lock:
                dirty[repo] = time.monotonic()
            return
        log = open(watcher_log_path(), "ab")  # noqa: SIM115 -- handed to the child
        running[repo] = subprocess.Popen(
            [sys.executable, "-m", "auryn", "sync", repo],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
        )
        logger.info("resync %s", repo)

    owns_pidfile = stop_event is None
    if stop_event is None:
        stop_event = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(ValueError):  # set handlers only on the main thread
                signal.signal(sig, lambda *_: stop_event.set())

    run_dir().mkdir(parents=True, exist_ok=True)  # for the resync log (+ pidfile)
    if not reconcile():
        return  # nothing to watch
    if owns_pidfile:
        watcher_pid_path().write_text(str(os.getpid()))
    last_reconcile = time.monotonic()
    try:
        while not stop_event.is_set():
            time.sleep(0.1)
            now = time.monotonic()
            with lock:
                ready = [r for r, t in dirty.items() if now - t >= debounce]
                for r in ready:
                    del dirty[r]
            for r in ready:
                spawn_sync(r)
            if now - last_reconcile >= reconcile_every:
                last_reconcile = now
                if not reconcile():
                    break  # no save-triggered repos left -> exit
    finally:
        for obs in observers.values():
            with contextlib.suppress(Exception):
                obs.stop()
        for obs in observers.values():
            with contextlib.suppress(Exception):
                obs.join(timeout=2)
        if owns_pidfile:
            with contextlib.suppress(OSError):
                if watcher_pid_path().read_text().strip() == str(os.getpid()):
                    watcher_pid_path().unlink()


# ── detached daemon lifecycle ──────────────────────────────────────────────────────────────
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
        return int(watcher_pid_path().read_text().strip())
    except (OSError, ValueError):
        return None


def is_watcher_running() -> bool:
    return _pid_alive(_read_pid())


def ensure_watcher() -> bool:
    """Start the detached watcher daemon if it isn't already up and there's something to watch.
    Returns True if it's running afterward."""
    if is_watcher_running():
        return True
    if not _save_repo_dirs():
        return False
    run_dir().mkdir(parents=True, exist_ok=True)
    log = open(watcher_log_path(), "ab")  # noqa: SIM115
    subprocess.Popen(
        [sys.executable, "-m", "auryn", "watch", "--foreground"],
        stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True,
    )
    deadline = time.time() + 10.0
    while time.time() < deadline:
        if is_watcher_running():
            return True
        time.sleep(0.1)
    return is_watcher_running()


def stop_watcher() -> bool:
    """Stop the watcher daemon if running. Returns False if nothing was running."""
    pid = _read_pid()
    if not _pid_alive(pid):
        with contextlib.suppress(OSError):
            watcher_pid_path().unlink()
        return False
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.time() + 5.0
    while time.time() < deadline and _pid_alive(pid):
        time.sleep(0.1)
    with contextlib.suppress(OSError):
        watcher_pid_path().unlink()
    return True
