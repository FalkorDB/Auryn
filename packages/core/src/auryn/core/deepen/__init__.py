"""The background deepen daemon: runs the deep (type-resolving) analysis pass off the ingest
critical path, so a graph is queryable at fast-pass speed and deepens in the background."""

from auryn.core.deepen.daemon import (
    daemon_supported,
    ensure_deepen_daemon,
    is_daemon_running,
    request_deepen,
    run_deepen_daemon,
    stop_deepen_daemon,
)

__all__ = [
    "daemon_supported",
    "ensure_deepen_daemon",
    "is_daemon_running",
    "request_deepen",
    "run_deepen_daemon",
    "stop_deepen_daemon",
]
