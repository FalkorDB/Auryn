"""A single factory for the process pools every ingestor's parse phase uses.

Centralises the start-method choice so no ingestor forks the parent directly. The parent holds an
open FalkorDB connection (its client runs background threads), so a plain ``fork`` -- the default
start method on Linux -- forks a *multi-threaded* process, which Python 3.12 warns about
(``DeprecationWarning: use of fork() may lead to deadlocks in the child``) and 3.14 will reject.

Preferring ``forkserver`` sidesteps this: workers are forked from a dedicated *single-threaded*
control process, so there's no fork-in-a-multi-threaded-process hazard -- while children still
inherit any ``preload``ed modules via copy-on-write, so a heavy parse import (jedi, a tree-sitter
grammar, ANTLR's generated tables) is paid once in the control process rather than re-imported per
worker. See make_process_pool for the fallback order.
"""

from __future__ import annotations

import multiprocessing
import os
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait

# Parse-fan-out tuning, shared by every tree-sitter language's full-recompute path (see
# parse_blobs_ordered). Mirrors the values the Python ingestor arrived at:
#   * below _PARALLEL_MIN_FILES files a pool's startup costs more than it saves -> parse inline.
#   * at most workers * _INFLIGHT_PER_WORKER source blobs are resident at once (read lazily at
#     submit time), so peak transient memory is bounded rather than the whole tree's bytes.
_PARALLEL_MIN_FILES = 8
_INFLIGHT_PER_WORKER = 3


def resolve_workers(requested: int | None = None) -> int:
    """The effective parse-pool size, unified across every ingestor. Precedence: an explicit
    `requested` (a command's --workers / config.workers) wins; else the `AURYN_PARSE_WORKERS`
    environment variable -- the one knob `auryn ingest --workers` sets for the whole run, honoured by
    EVERY pool-based parser (python, c#, pl/sql, oraschema); else `os.cpu_count()`. Always >= 1; a
    non-positive or unparseable value in either source falls through to the next."""
    if requested and requested > 0:
        return requested
    env = os.environ.get("AURYN_PARSE_WORKERS", "").strip()
    if env:
        try:
            n = int(env)
            if n > 0:
                return n
        except ValueError:
            pass  # a stray value must never break an ingest; fall through to the CPU count
    return os.cpu_count() or 1


def make_process_pool(
    workers: int,
    *,
    preload: Sequence[str] = (),
    max_tasks_per_child: int | None = None,
) -> ProcessPoolExecutor:
    """A parse pool that never forks the multi-threaded parent directly.

    Start-method preference: ``forkserver`` (workers forked from a single-threaded control process,
    with ``preload``ed modules inherited via COW) -> ``fork`` -> the platform default (``spawn`` on
    Windows, where neither POSIX method exists).

    ``max_tasks_per_child`` (recycle a worker after N tasks, to cap unbounded per-worker memory
    growth) is INCOMPATIBLE with ``fork`` -- passing it under a ``fork`` context raises, and passing
    it with no explicit context makes ProcessPoolExecutor silently fall back to ``spawn`` (which
    re-imports ``preload`` per worker, defeating the point). So when it's set the middle ``fork``
    fallback is skipped: ``forkserver`` -> ``spawn``.

    ``preload`` is a pure speed optimisation -- a module that fails to preload is simply imported
    on demand in each worker -- so correctness never depends on the start method actually chosen.
    """
    methods = ("forkserver", "spawn") if max_tasks_per_child is not None else ("forkserver", "fork")
    context = None
    for method in methods:
        try:
            context = multiprocessing.get_context(method)
        except ValueError:
            continue  # method unavailable on this platform (fork/forkserver are POSIX-only)
        if method == "forkserver" and preload:
            try:
                context.set_forkserver_preload(list(preload))
            except (ValueError, ImportError):
                pass  # optimisation only -- fall through and let workers import on demand
        break

    # context is None only when no POSIX start method exists (Windows): omit mp_context so
    # ProcessPoolExecutor uses its platform default (spawn).
    kwargs: dict = {"max_workers": workers}
    if context is not None:
        kwargs["mp_context"] = context
    if max_tasks_per_child is not None:
        kwargs["max_tasks_per_child"] = max_tasks_per_child
    return ProcessPoolExecutor(**kwargs)


def parse_blobs_ordered(
    entries: Sequence[tuple[str, str]],
    read_blob: Callable[[str], bytes],
    parse: Callable[[bytes, str], object],
    *,
    preload: Sequence[str] = (),
    logger=None,
    workers: int | None = None,
    sink: Callable[[tuple[str, str], object], None] | None = None,
    min_parallel_files: int = _PARALLEL_MIN_FILES,
    inflight_per_worker: int = _INFLIGHT_PER_WORKER,
) -> list | None:
    """Parse ``(path, blob_sha)`` entries, fanning the CPU-bound tree-sitter parse across a process
    pool. Returns results **in input order** -- so node ids stay deterministic.

    This is the one place every git-driven language's full-recompute parse loop shares, so the
    forkserver start method, the memory-bounding, the parallel threshold and the verbose-safe serial
    fallback are decided once rather than re-implemented (subtly differently) per language.

    ``parse`` MUST be a module-level (picklable-by-reference) ``callable(blob_bytes, path) -> result``
    returning a picklable value (the ingestors return plain ``RawFile`` dataclasses -- no tree-sitter
    node crosses the process boundary). ``read_blob(blob_sha) -> bytes`` runs in the *parent* (git
    I/O) and is called lazily as each task is submitted, so at most ``workers * inflight_per_worker``
    blobs are resident at once. ``preload`` names modules to import into the forkserver control
    process (typically the one defining ``parse``) so a heavy grammar import is paid once, inherited
    by workers via copy-on-write, rather than re-imported per worker -- see make_process_pool.

    ``sink``: when given, each ``(entry, result)`` is handed to ``sink`` (in the parent) as it is
    produced and NOT retained, and the function returns ``None`` -- for callers that persist results
    incrementally (e.g. a disk parse cache) and must not hold the whole parsed tree in memory. Note
    ``sink`` sees results in COMPLETION order, not input order. Without ``sink`` the results are
    collected and returned as a list in input order.

    Parses **serially** (logging each file *before* parsing it) when there are few files, only one
    worker, or verbose mode is on: a native parser crash then leaves the offending file as the last
    logged line, which a pool with several files in flight could not pinpoint.
    """
    from auryn.core import progress  # local: keep this module import-light for CLI startup

    entries = list(entries)
    n = len(entries)
    workers = resolve_workers(workers)  # honour AURYN_PARSE_WORKERS when the caller didn't specify

    if workers <= 1 or n <= min_parallel_files or progress.verbose_enabled():
        results: list | None = None if sink is not None else []
        for i, entry in enumerate(entries):
            path, blob_sha = entry
            if logger is not None:
                progress.log_file(logger, path, i + 1, n)
            result = parse(read_blob(blob_sha), path)
            if sink is not None:
                sink(entry, result)
            else:
                results.append(result)
        return results

    results = None if sink is not None else [None] * n
    max_inflight = workers * inflight_per_worker
    with make_process_pool(workers, preload=preload) as pool:
        inflight: dict = {}  # Future -> input index
        next_idx = 0

        def submit(i: int) -> None:
            path, blob_sha = entries[i]
            inflight[pool.submit(parse, read_blob(blob_sha), path)] = i

        while next_idx < n and len(inflight) < max_inflight:
            submit(next_idx)
            next_idx += 1
        while inflight:
            done, _ = wait(inflight, return_when=FIRST_COMPLETED)
            for fut in done:
                i = inflight.pop(fut)
                result = fut.result()  # re-raises a worker error, as map would
                if sink is not None:
                    sink(entries[i], result)  # drop after handing off -- keeps memory bounded
                else:
                    results[i] = result
                if next_idx < n:
                    submit(next_idx)
                    next_idx += 1
    return results
