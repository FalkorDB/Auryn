"""Paths under the Auryn home directory (default ``~/.auryn``).

One central folder holds everything Auryn creates on a machine -- durable state at the
top (``config.toml``, the ``store.db`` RDB) and ephemeral runtime state under ``run/`` -- so a
full uninstall is "remove one directory", and nothing is ever written into a user's repo.

The root is overridable with ``AURYN_HOME`` (tests and non-default installs). See
docs/adr/0001-onboarding-and-server-lifecycle.md.
"""

from __future__ import annotations

import os
from pathlib import Path

_ENV_HOME = "AURYN_HOME"


def home_dir() -> Path:
    """The Auryn home directory. ``AURYN_HOME`` overrides the ``~/.auryn``
    default. Not created here -- callers that write create the specific subdir they need."""
    override = os.environ.get(_ENV_HOME)
    return Path(override).expanduser() if override else Path.home() / ".auryn"


def config_path() -> Path:
    """Durable, user-owned config: the chosen backend and the install manifest."""
    return home_dir() / "config.toml"


def store_db_path() -> Path:
    """The RDB the local (falkordb-lite) backend persists to -- one file holding every
    repo's graph as a separately-named FalkorDB graph."""
    return home_dir() / "store.db"


def hooks_dir() -> Path:
    """User-authored post-hook modules, auto-discovered and run after ingestion (full and
    incremental). Each ``*.py`` defines a top-level ``run(writer, repo, target_sha)`` (see
    core/registry._load_local_hooks). Machine-wide and user-owned -- never read from a repo,
    so ingesting an untrusted clone can't execute code. Not created here; `auryn hooks new`
    and discovery create it on demand."""
    return home_dir() / "hooks"


def usage_log_path() -> Path:
    """Durable, append-only JSONL log of agent tool calls against the MCP server -- one event per
    graph access (see core/usage.py). Lives at the top of the home dir (not under run/) so it
    survives across sessions and reboots; `auryn stats` aggregates it. Local-only, never sent
    anywhere."""
    return home_dir() / "usage.jsonl"


def run_dir() -> Path:
    """Ephemeral runtime state (rendezvous file, lock, daemon log). Safe to delete; it is
    recreated on the next local-server start."""
    return home_dir() / "run"


def server_info_path() -> Path:
    """Rendezvous file the local daemon writes and clients read to discover its random port."""
    return run_dir() / "server.json"


def server_lock_path() -> Path:
    """fcntl lock file serializing concurrent cold-starts of the local daemon."""
    return run_dir() / "server.lock"


def serve_log_path() -> Path:
    """Where the detached local daemon's stdout/stderr are redirected (it must not inherit a
    caller's stdio -- an MCP server's stdio *is* its protocol stream)."""
    return run_dir() / "serve.log"


def watcher_pid_path() -> Path:
    """PID file the save-trigger watcher daemon writes (owns it for its lifetime)."""
    return run_dir() / "watcher.pid"


def watcher_log_path() -> Path:
    """Where the detached watcher daemon's stdout/stderr go."""
    return run_dir() / "watcher.log"


def embed_dir() -> Path:
    """Runtime state for the background embedding daemon (lock, pidfile, log, request queue).
    One daemon per machine builds every repo's vectors off the ingest critical path."""
    return run_dir() / "embed"


def embed_lock_path() -> Path:
    """fcntl run-lock the embed daemon holds for its whole life -- single-flight: a second
    daemon that can't acquire it exits, so there's never more than one (and the model loads once)."""
    return embed_dir() / "daemon.lock"


def embed_pid_path() -> Path:
    """PID file the embed daemon writes; producers read it to decide whether to spawn one."""
    return embed_dir() / "daemon.pid"


def embed_log_path() -> Path:
    """Where the detached embed daemon's stdout/stderr go."""
    return embed_dir() / "daemon.log"


def embed_requests_dir() -> Path:
    """The embed daemon's work queue: one ``<graph>.json`` file per graph with pending vectors.
    The many-producers -> one-consumer seam (an ingest drops a request; the daemon drains it)."""
    return embed_dir() / "requests"
