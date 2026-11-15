"""Opt-in per-file progress logging for an ingest.

A normal ingest logs only milestones (per-language begin/end, totals) -- enough to
know it finished, not enough to watch it *work*. On a big source (tens of thousands of
files) or a wedged parser, that leaves a long silent gap with no sign of life and, worse,
no clue which file a crashing parser choked on. `--verbose` (or `AURYN_VERBOSE=1`)
turns on a line per processed file, streamed live as each is parsed.

Enabled state lives in an environment variable, not a module global, on purpose: it has to
survive into the subprocess workers the C#/PL/SQL ingestors fork and any child the MCP server
spawns, which a module global wouldn't. `--verbose` calls set_verbose(True), which exports it;
`verbose_enabled()` reads it back anywhere in the process tree.

When off, `log_file()` is a single env lookup + branch, so the per-file calls threaded through
every parse loop cost effectively nothing on a normal run.
"""

from __future__ import annotations

import logging
import os

_ENV_VAR = "AURYN_VERBOSE"
_OFF = frozenset({"", "0", "false", "no", "off"})


def set_verbose(enabled: bool) -> None:
    """Turn per-file logging on (or off) for this process and every child it spawns.
    Called by the CLI's `--verbose` flag; exports AURYN_VERBOSE so forked parse
    workers and MCP-spawned children inherit the setting."""
    if enabled:
        os.environ[_ENV_VAR] = "1"
    else:
        os.environ.pop(_ENV_VAR, None)


def verbose_enabled() -> bool:
    """Whether per-file logging is on, from AURYN_VERBOSE (set by --verbose or the env)."""
    return os.environ.get(_ENV_VAR, "").strip().lower() not in _OFF


def log_file(logger: logging.Logger, path: str, index: int | None = None, total: int | None = None) -> None:
    """Log that `path` is about to be processed -- but only in verbose mode. Pass the 1-based
    `index` and `total` when known so the line doubles as a progress counter (`[123/589] ...`).

    Log BEFORE the work, not after: if the parser crashes the process (a segfault in a native
    accelerator, an OOM), the last line emitted names the offending file -- which is the whole
    point of watching a wedged run."""
    if not verbose_enabled():
        return
    if index is not None and total is not None:
        logger.info("[%d/%d] parsing %s", index, total, path)
    else:
        logger.info("parsing %s", path)
