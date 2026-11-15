"""Optional, off-by-default per-phase timing for a sync -- so a benchmark (or a
curious caller) can see *where* a sync spends its wall-clock, not just the single
total number SyncStats already reports.

A sync's cost decomposes into two halves: recompute the desired target state
(parse the files + resolve cross-references) and reconcile it with the graph (read
back the current fingerprints + diff + write the delta). The read-back has been
profiled at ~87% of an incremental resync (see graph_client._SUBGRAPH_READ_WORKERS),
so attributing time to these phases is the only way to know which optimization
would actually move the needle for a given change.

Design: a single `ContextVar` holds the active recorder (a name -> seconds
accumulator) for the duration of a `record_phases()` block. The shared write
primitives (GraphWriter.read_fingerprints / .apply_delta, graph_sync.compute_delta)
and -- for the flagship language -- the Python engine's parse/resolve steps wrap
their bodies in `phase(name)`, which adds to the active recorder if one is set and
is otherwise a near-free no-op (one ContextVar lookup + a branch). Nothing changes
about a normal, un-recorded sync.

Threading note: `phase()` only accumulates on the thread that entered
`record_phases()`. read_fingerprints fans its per-label queries across an internal
ThreadPoolExecutor, but the *outer* call it wraps runs on the sync thread, so its
whole wall-clock is captured correctly; a background deep pass on another thread
(deep="background") simply isn't recorded, which is what we want.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar

# The active accumulator, or None when nobody is recording. Keyed by phase name,
# valued in seconds (summed across every `phase(name)` block in the run).
_active: ContextVar[dict[str, float] | None] = ContextVar("ingestor_phase_recorder", default=None)


@contextmanager
def record_phases():
    """Collect per-phase timings for everything that runs inside this block. Yields the
    accumulator dict (name -> seconds), populated by the time the block exits. Re-entrant:
    a nested `record_phases()` shadows the outer one for its own scope and restores it
    after (the outer accumulator simply doesn't see the inner block's phases)."""
    acc: dict[str, float] = {}
    token = _active.set(acc)
    try:
        yield acc
    finally:
        _active.reset(token)


@contextmanager
def phase(name: str):
    """Time this block into the active recorder under `name` (summing across repeated
    blocks with the same name). A no-op with negligible overhead when no `record_phases()`
    is active -- the common case for a normal sync."""
    acc = _active.get()
    if acc is None:
        yield
        return
    start = time.perf_counter()
    try:
        yield
    finally:
        acc[name] = acc.get(name, 0.0) + (time.perf_counter() - start)
