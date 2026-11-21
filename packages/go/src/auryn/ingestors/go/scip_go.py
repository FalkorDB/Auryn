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
import tempfile

from auryn.core import scip_runner

_SCIP_TIMEOUT_SECONDS = int(os.environ.get("AURYN_SCIP_GO_TIMEOUT", "900"))

_INSTALL_HINT = (
    "scip-go not found on PATH. Install it with "
    "`go install github.com/scip-code/scip-go/cmd/scip-go@latest` (needs Go >=1.26, or GOTOOLCHAIN=auto), "
    "or set AURYN_SCIP_GO to its path."
)


class ScipGoUnavailable(scip_runner.ScipIndexerUnavailable):
    """scip-go could not be located or failed to produce a SCIP index -- the signal for git_sync to fall
    back to the tree-sitter fast pass."""


def _tool_command() -> list[str]:
    """Locate a usable scip-go from a TRUSTED location: `AURYN_SCIP_GO` override, else `scip-go` on PATH
    (a user-controlled `go install`). See scip_runner.resolve_trusted_tool for the trust model."""
    return scip_runner.resolve_trusted_tool(
        env_var="AURYN_SCIP_GO", which="scip-go", unavailable=ScipGoUnavailable, hint=_INSTALL_HINT)


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
    if scip_runner.is_inside(cmd[0], root):  # never run a scip-go that lives inside the (untrusted) tree
        raise ScipGoUnavailable(f"refusing to run scip-go from inside the indexed tree: {cmd[0]}")
    env = {**os.environ}
    env.setdefault("GOTOOLCHAIN", "auto")
    with tempfile.TemporaryDirectory() as tmp:
        out_path = os.path.join(tmp, "index.scip")
        argv = [*cmd, "index", "-q", f"--module-root={root}", "-o", out_path]
        return scip_runner.run_indexer(argv, cwd=root, out_path=out_path, timeout=_SCIP_TIMEOUT_SECONDS,
                                       tool="scip-go", unavailable=ScipGoUnavailable, env=env)
