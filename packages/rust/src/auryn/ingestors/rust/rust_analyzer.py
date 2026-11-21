"""Deep Rust call resolution via rust-analyzer's SCIP index (the `deep_sync` engine, mirroring C#'s
Roslyn and Python's jedi deep passes).

The fast tree-sitter pass resolves CALLS *by name*, so a method on a `dyn`/`impl`/generic receiver, an
untyped local, or a cross-module/crate callee is dropped or guessed (see `unresolved_calls`, #792/#794).
rust-analyzer resolves them precisely. We ask it for a SCIP index (`rust-analyzer scip <root>`); the
index is then turned into resolved CALLS by the shared, language-agnostic `core.scip_resolve` (occurrence
containment over the fast pass's `CallableLocation`s) -- the same code scip-typescript and other SCIP
indexers use. This module is just the Rust-specific RUNNER: locate rust-analyzer and produce the
`index.scip` bytes.

Graceful degradation is the contract (as with Roslyn's `RoslynUnavailable`): if rust-analyzer isn't
available or the crate can't be analyzed, `RustAnalyzerUnavailable` is raised and git_sync falls back to
the tree-sitter fast pass. v1 resolves INTRA-project calls only and leaves the fast pass's `extern://`
placeholders untouched (so stdlib/other-crate edges and cross-repo reconciliation are preserved)."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

# Re-exported for the engine + tests: the SCIP->CALLS derivation is shared infrastructure (core), not
# Rust-specific. git_sync calls rust_analyzer.resolve_calls; test_rust_deep drives the helpers here.
from auryn.core.scip_resolve import (  # noqa: F401
    DeepCalls,
    _is_callable_symbol,
    _simple_name,
    resolve_calls,
)

_SCIP_TIMEOUT_SECONDS = int(os.environ.get("AURYN_RUST_ANALYZER_TIMEOUT", "900"))


class RustAnalyzerUnavailable(RuntimeError):
    """rust-analyzer could not be located or failed to produce a SCIP index -- the signal for git_sync
    to fall back to the tree-sitter fast pass."""


def _tool_command() -> list[str]:
    """Locate a usable rust-analyzer, in priority order (mirrors roslyn._tool_command):
      1. `AURYN_RUST_ANALYZER` env override (an explicit binary path);
      2. `rustup which rust-analyzer` -- resolves the real toolchain binary only when the component is
         actually installed (the `~/.cargo/bin/rust-analyzer` proxy exists even when it isn't);
      3. a plain `rust-analyzer` on PATH (Homebrew, a manual install, or a working rustup proxy).
    Raises RustAnalyzerUnavailable (with the one-line `rustup component add` hint) if none resolve. The
    chosen command is still validated by use -- a proxy whose component is missing fails at scip time
    and is turned into RustAnalyzerUnavailable there."""
    override = os.environ.get("AURYN_RUST_ANALYZER")
    if override:
        return [override]
    if shutil.which("rustup"):
        try:
            got = subprocess.run(["rustup", "which", "rust-analyzer"], capture_output=True, text=True,
                                 timeout=30, check=False)
            path = got.stdout.strip()
            if got.returncode == 0 and path and os.path.exists(path):
                return [path]
        except (OSError, subprocess.SubprocessError):
            pass  # rustup missing/broken -> fall through to a plain PATH lookup below
    found = shutil.which("rust-analyzer")
    if found:
        return [found]
    raise RustAnalyzerUnavailable(
        "rust-analyzer not found. Install it with `rustup component add rust-analyzer`, "
        "or set AURYN_RUST_ANALYZER to its path."
    )


def run_scip(root: str) -> bytes:
    """Run `rust-analyzer scip <root>` and return the raw `index.scip` bytes. Raises
    RustAnalyzerUnavailable on any failure (tool missing, non-zero exit, unbuildable crate, no output)
    so the caller can fall back to the fast pass. `root` must be a cargo project directory on disk."""
    cmd = _tool_command()
    with tempfile.TemporaryDirectory() as tmp:
        out_path = os.path.join(tmp, "index.scip")
        argv = [*cmd, "scip", root, "--output", out_path]
        try:
            proc = subprocess.run(
                argv, cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                timeout=_SCIP_TIMEOUT_SECONDS, check=False,
            )
        except OSError as exc:  # FileNotFoundError, PermissionError (non-executable override), etc.
            raise RustAnalyzerUnavailable(f"could not execute {argv[0]!r}: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise RustAnalyzerUnavailable(f"rust-analyzer scip timed out after {_SCIP_TIMEOUT_SECONDS}s") from exc
        # Older rust-analyzer ignores --output and writes index.scip into CWD; accept either.
        produced = out_path if os.path.exists(out_path) else os.path.join(root, "index.scip")
        if proc.returncode != 0 or not os.path.exists(produced):
            tail = (proc.stderr or b"").decode("utf-8", "replace")[-600:]
            raise RustAnalyzerUnavailable(f"rust-analyzer scip exited {proc.returncode}: {tail}")
        with open(produced, "rb") as fh:
            data = fh.read()
        if produced != out_path:  # clean up the CWD artifact from the fallback path
            try:
                os.remove(produced)
            except OSError:
                pass  # best-effort cleanup -- a leftover index.scip in the checkout is harmless
    return data
