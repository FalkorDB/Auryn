"""Deep Go call resolution via Sourcegraph's scip-go indexer (the `deep_sync` engine, mirroring Rust's
rust-analyzer, C#'s Roslyn, Python's jedi, and TypeScript's scip-typescript deep passes).

The fast tree-sitter pass resolves CALLS *by name*, so a method on an interface/inferred receiver, or a
cross-package callee that collides with a same-named local, is dropped or guessed. scip-go resolves them
precisely via go/types. We ask it for a SCIP index (`scip-go index`); the index is turned into resolved
CALLS by the shared, language-agnostic `core.scip_resolve` (occurrence containment over the fast pass's
`CallableLocation`s) -- the same code the Rust/TS deep passes use. This module is just the Go-specific
RUNNER: locate scip-go and produce the `index.scip` bytes.

Graceful degradation is the contract (as with the other deep engines): if scip-go isn't available or the
module can't be loaded, `ScipGoUnavailable` is raised and git_sync falls back to the tree-sitter fast
pass. v1 resolves INTRA-project calls only over a single module (`--module-root`); a nested/multi-module
repo whose SCIP paths don't line up with the repo-relative paths simply resolves fewer calls and is a
follow-up. Needs a Go toolchain (>=1.26, or GOTOOLCHAIN=auto to auto-provision one)."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

_SCIP_TIMEOUT_SECONDS = int(os.environ.get("AURYN_SCIP_GO_TIMEOUT", "900"))


class ScipGoUnavailable(RuntimeError):
    """scip-go could not be located or failed to produce a SCIP index -- the signal for git_sync to fall
    back to the tree-sitter fast pass."""


def _tool_command() -> list[str]:
    """Locate a usable scip-go from a TRUSTED location ONLY:
      1. `AURYN_SCIP_GO` env override (an explicit, user-trusted binary path);
      2. a `scip-go` on PATH (a user-controlled `go install .../scip-go` into GOPATH/bin).
    Returned as an ABSOLUTE realpath (symlinks resolved) so the subprocess never re-resolves it against
    its cwd and run_scip can refuse a binary inside the indexed tree -- deep analysis runs automatically on
    arbitrary (possibly untrusted) repositories, so a repo-shipped binary must never execute. Raises
    ScipGoUnavailable if no trusted binary resolves."""
    candidate = os.environ.get("AURYN_SCIP_GO") or shutil.which("scip-go")
    if candidate:
        resolved = os.path.realpath(candidate)
        if os.path.isfile(resolved):
            return [resolved]
    raise ScipGoUnavailable(
        "scip-go not found on PATH. Install it with "
        "`go install github.com/scip-code/scip-go/cmd/scip-go@latest` (needs Go >=1.26, or GOTOOLCHAIN=auto), "
        "or set AURYN_SCIP_GO to its path."
    )


def _is_inside(path: str, root: str) -> bool:
    root_real = os.path.realpath(root)
    try:
        return os.path.commonpath([os.path.realpath(path), root_real]) == root_real
    except ValueError:  # different drives (Windows) -> not inside
        return False


def run_scip(root: str) -> bytes:
    """Run `scip-go index` over the module at `root` and return the raw `index.scip` bytes. Raises
    ScipGoUnavailable on any failure (tool missing, timeout, no output) so the caller can fall back to the
    fast pass. `root` must be a Go module directory (a go.mod) on disk.

    `--module-root=root -o <tmp>` keeps the output out of the indexed tree (scip-go writes no stray files,
    unlike scip-typescript); `-q` keeps stdout clean. scip-go's exit code is lenient (it emits a partial
    index even when some packages don't type-check), so success is gated on a non-empty index file
    existing -- whether that index actually analyzed our callables is decided downstream (git_sync falls
    back if SCIP covered nothing). GOTOOLCHAIN=auto lets a host Go older than scip-go's floor (>=1.26)
    provision the required toolchain. (We deliberately pass no --module-version/--repository-remote: those
    made scip-go emit an occurrence-less index here; it defaults version to the git short-sha, or "." with
    no git, which is fine for the containment join -- paths, not versions, drive it.)"""
    cmd = _tool_command()
    if _is_inside(cmd[0], root):  # never run a scip-go that lives inside the (untrusted) indexed tree
        raise ScipGoUnavailable(f"refusing to run scip-go from inside the indexed tree: {cmd[0]}")
    env = {**os.environ}
    env.setdefault("GOTOOLCHAIN", "auto")
    with tempfile.TemporaryDirectory() as tmp:
        out_path = os.path.join(tmp, "index.scip")
        argv = [*cmd, "index", "-q", f"--module-root={root}", "-o", out_path]
        try:
            proc = subprocess.run(
                argv, cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, timeout=_SCIP_TIMEOUT_SECONDS, check=False, env=env,
            )
        except OSError as exc:  # FileNotFoundError, PermissionError (non-executable override), etc.
            raise ScipGoUnavailable(f"could not execute {argv[0]!r}: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ScipGoUnavailable(f"scip-go timed out after {_SCIP_TIMEOUT_SECONDS}s") from exc
        if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            tail = (proc.stderr or b"").decode("utf-8", "replace")[-600:]
            raise ScipGoUnavailable(f"scip-go index produced no output (exit {proc.returncode}): {tail}")
        with open(out_path, "rb") as fh:
            return fh.read()
