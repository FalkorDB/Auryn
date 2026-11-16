"""One resync per repo at a time (ADR 0003: "a per-repo lock in `run/`").

Every git hook fires `auryn sync <repo> &`, so a rebase or a burst of commits starts many at
once -- 17 from one 15-commit rebase. Unserialized they interleave their reads and writes of the
same project's subgraph and leave it wrong (2 of 31 functions) while the last one to finish
reports it current. `sync_lock` makes them take turns, and sends all but ONE waiter home: a sync
that starts after the one in progress reads the working tree as it is then, so a single queued
sync already covers every trigger that arrived in the meantime.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path

try:
    import fcntl
except ImportError:  # native Windows -- no fcntl; syncs there stay unserialized, as before
    fcntl = None

from auryn.core.home import run_dir
from auryn.core.project import default_graph_name


@contextlib.contextmanager
def sync_lock(local_dir: Path) -> Iterator[bool]:
    """Yields True once it is this sync's turn for `local_dir` (blocking while another runs), or
    False straight away when a sync of it is already waiting its turn -- this one is redundant."""
    if fcntl is None:
        yield True
        return
    run_dir().mkdir(parents=True, exist_ok=True)
    stem = run_dir() / f"sync-{default_graph_name(local_dir)}"  # basename + a hash of the path
    with open(f"{stem}.queued", "w") as queued, open(f"{stem}.lock", "w") as running:
        try:
            fcntl.flock(queued, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        fcntl.flock(running, fcntl.LOCK_EX)  # wait out the sync in progress, if any
        fcntl.flock(queued, fcntl.LOCK_UN)  # running now: the next trigger may queue behind us
        try:
            yield True
        finally:
            fcntl.flock(running, fcntl.LOCK_UN)
