"""Shared plumbing for the per-language SCIP indexer RUNNERS (scip-go, scip-clang, scip-java,
scip-typescript, rust-analyzer). Each language's deep pass has a `run_scip(root) -> bytes` that locates its
indexer and produces an `index.scip`; the bytes then go to the language-agnostic `core.scip_resolve`. The
*locating* and *running* is near-identical boilerplate -- and it is SECURITY-SENSITIVE (deep analysis runs
automatically on arbitrary, possibly untrusted repositories), so it belongs in ONE audited place rather
than copied five times. This module owns:

  * `ScipIndexerUnavailable` -- the base signal that tells git_sync to fall back to the fast tree-sitter
    pass. Each language subclasses it (for a specific name + install hint) so existing `except ScipXUnavailable`
    handlers keep working, while a generic caller can catch the base.
  * `resolve_trusted_tool(...)` -- resolve an indexer binary from a TRUSTED location only: an explicit env
    override, or (opt-in) PATH. Always returned as an absolute realpath (symlinks resolved) so the
    subprocess never re-resolves it against the indexed repo's cwd. A tool that executes the repo's own
    build/compiler flags (scip-java, scip-clang) passes `which=None` -- PATH auto-discovery is refused so
    running untrusted code stays a deliberate per-environment opt-in.
  * `is_inside(path, root)` -- the containment check a runner uses to refuse a binary that resolves INSIDE
    the indexed tree (a repo-shipped indexer must never execute).
  * `run_indexer(...)` -- run the indexer subprocess and return the index bytes, turning a missing tool /
    timeout / empty (or, when demanded, non-zero-exit) index into `ScipIndexerUnavailable`.

A language keeps its own `run_scip` for genuinely per-tool behavior (rust-analyzer's rustup discovery and
old-binary output fallback; scip-typescript's stray-file cleanup in a live tree) and reuses whatever here
fits."""

from __future__ import annotations

import os
import shutil
import subprocess


class ScipIndexerUnavailable(RuntimeError):
    """A SCIP indexer could not be located or failed to produce a usable index -- the signal for a deep
    pass to fall back to the tree-sitter fast pass. Each language subclasses this with its own install hint."""


def is_inside(path: str, root: str) -> bool:
    """True if `path` resolves inside `root` (both realpath'd, so a symlinked binary or checkout can't slip
    the check). Used to refuse running an indexer binary shipped inside the (untrusted) indexed tree."""
    root_real = os.path.realpath(root)
    try:
        return os.path.commonpath([os.path.realpath(path), root_real]) == root_real
    except ValueError:  # different drives (Windows) -> not inside
        return False


def resolve_trusted_tool(*, env_var: str, which: str | None, unavailable: type[ScipIndexerUnavailable],
                         hint: str) -> list[str]:
    """Resolve an indexer binary to `[absolute_realpath]` from a trusted location: the `env_var` override
    first, then -- only when `which` is given -- a `which`-named binary on PATH. `which=None` means opt-in
    ONLY (no PATH discovery): for an indexer that executes the repo's build/compiler flags, auto-discovery
    would silently run untrusted code. The realpath (not just abspath) resolves symlinks, so a trusted-
    looking path that is actually a symlink into the indexed tree is still caught by `is_inside` at run time.
    Raises `unavailable(hint)` if nothing trusted resolves."""
    candidate = os.environ.get(env_var)
    if not candidate and which:
        candidate = shutil.which(which)
    if candidate:
        resolved = os.path.realpath(candidate)
        if os.path.isfile(resolved):
            return [resolved]
    raise unavailable(hint)


def run_indexer(argv: list[str], *, cwd: str, out_path: str, timeout: int, tool: str,
                unavailable: type[ScipIndexerUnavailable], env: dict | None = None,
                require_zero_exit: bool = False) -> bytes:
    """Run a SCIP indexer subprocess and return the bytes it wrote to `out_path`. The subprocess is run with
    stdin/stdout to DEVNULL and stderr captured (its tail is surfaced on failure). A missing executable, a
    timeout, or a missing/empty output file becomes `unavailable`; `require_zero_exit=True` also rejects a
    non-zero exit (for an indexer that otherwise exits 0 while indexing best-effort through diagnostics, so a
    non-zero exit is a real failure whose index may be truncated). The caller is responsible for refusing a
    binary inside the tree (via `is_inside`) before calling -- it knows which root is untrusted (e.g. a build
    sandbox runs with `cwd` = a throwaway copy, but the refusal must check the original source)."""
    try:
        proc = subprocess.run(
            argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, timeout=timeout, check=False, env=env,
        )
    except OSError as exc:  # FileNotFoundError, PermissionError (non-executable override), etc.
        raise unavailable(f"could not execute {argv[0]!r}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise unavailable(f"{tool} timed out after {timeout}s") from exc
    if ((require_zero_exit and proc.returncode != 0)
            or not os.path.exists(out_path) or os.path.getsize(out_path) == 0):
        tail = (proc.stderr or b"").decode("utf-8", "replace")[-600:]
        raise unavailable(f"{tool} produced no usable index (exit {proc.returncode}): {tail}")
    with open(out_path, "rb") as fh:
        return fh.read()
