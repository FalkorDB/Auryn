"""Deep TypeScript/JavaScript call resolution via Sourcegraph's scip-typescript indexer (the `deep_sync`
engine, mirroring Rust's rust-analyzer, C#'s Roslyn, and Python's jedi deep passes).

The fast tree-sitter pass resolves CALLS *by name*, so a method on an untyped/inferred receiver, an
imported/re-exported callee, or two same-named methods on different classes are dropped or guessed.
scip-typescript resolves them precisely via the TypeScript compiler. We ask it for a SCIP index
(`scip-typescript index`); the index is turned into resolved CALLS by the shared, language-agnostic
`core.scip_resolve` (occurrence containment over the fast pass's `CallableLocation`s) -- the same code
the Rust deep pass uses. This module is just the TS-specific RUNNER: locate scip-typescript and produce
the `index.scip` bytes.

Graceful degradation is the contract (as with RustAnalyzerUnavailable / RoslynUnavailable): if
scip-typescript isn't available or can't analyze the project, `ScipTypescriptUnavailable` is raised and
git_sync falls back to the tree-sitter fast pass. v1 resolves INTRA-project calls only (no `npm install`
is run, so calls whose resolution needs a third-party package's types stay name-based), over a single
project (monorepo workspace flags are a follow-up), and leaves the fast pass's `extern://` placeholders
untouched. Arrow-function consts (`const f = () => {}`) carry a SCIP *term* descriptor, not `().`, so
they are not deep-upgraded -- their fast name-edges are kept (no regression)."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

_SCIP_TIMEOUT_SECONDS = int(os.environ.get("AURYN_SCIP_TYPESCRIPT_TIMEOUT", "900"))

_PACKAGE = "@sourcegraph/scip-typescript"


class ScipTypescriptUnavailable(RuntimeError):
    """scip-typescript could not be located or failed to produce a SCIP index -- the signal for git_sync
    to fall back to the tree-sitter fast pass."""


def _tool_command(root: str) -> list[str]:
    """Locate a usable scip-typescript, in priority order (mirrors rust_analyzer._tool_command):
      1. `AURYN_SCIP_TYPESCRIPT` env override (an explicit binary path);
      2. the project's own `node_modules/.bin/scip-typescript` (a repo that depends on it directly);
      3. a plain `scip-typescript` on PATH (a global `npm i -g @sourcegraph/scip-typescript`);
      4. `npx --no-install @sourcegraph/scip-typescript` (installed somewhere npx can resolve, without
         letting npx silently download it over the network).
    Raises ScipTypescriptUnavailable (with an install hint) if none resolve. The chosen command is still
    validated by use -- an npx that can't resolve the package fails at index time and is turned into
    ScipTypescriptUnavailable there."""
    override = os.environ.get("AURYN_SCIP_TYPESCRIPT")
    if override:
        return [override]
    local = os.path.join(root, "node_modules", ".bin", "scip-typescript")
    if os.path.exists(local):
        return [local]
    found = shutil.which("scip-typescript")
    if found:
        return [found]
    npx = shutil.which("npx")
    if npx:
        return [npx, "--no-install", _PACKAGE]
    raise ScipTypescriptUnavailable(
        f"scip-typescript not found. Install it with `npm install -g {_PACKAGE}` "
        f"(or add it to the project's devDependencies), or set AURYN_SCIP_TYPESCRIPT to its path."
    )


def run_scip(root: str) -> bytes:
    """Run `scip-typescript index` over `root` and return the raw `index.scip` bytes. Raises
    ScipTypescriptUnavailable on any failure (tool missing, non-zero exit, no output) so the caller can
    fall back to the fast pass. `root` must be a project directory on disk.

    `--infer-tsconfig` lets config-less / JS-only projects index; `--no-progress-bar` keeps the subprocess
    quiet. scip-typescript's exit code is lenient (it indexes best-effort through type errors), so success
    is gated on a non-empty index file existing -- and whether that index actually analyzed our callables
    is decided downstream (git_sync falls back if SCIP covered nothing).

    scip-typescript mutates the indexed dir: it can write a stray `index.scip` (regardless of `--output`),
    and `--infer-tsconfig` writes an inferred `tsconfig.json` when none exists. For a git source the dir is
    a throwaway checkout, but for a LOCAL-directory source it is the user's LIVE tree. So we snapshot which
    of those files already existed and afterward remove ONLY the ones we caused to appear -- never the
    user's own `index.scip` / `tsconfig.json` / `jsconfig.json`, and never reading a pre-existing
    `index.scip` as if it were our output. (A config created by an unrelated process DURING indexing is an
    inherent, narrow race; auryn's own ingests are serialized per repo by sync_lock.)"""
    cmd = _tool_command(root)
    stray = os.path.join(root, "index.scip")
    artifacts = [stray, os.path.join(root, "tsconfig.json"), os.path.join(root, "jsconfig.json")]
    pre_existing = {p for p in artifacts if os.path.exists(p)}

    def _cleanup_artifacts() -> None:
        for p in artifacts:
            if p not in pre_existing:  # only what WE caused to appear -- never the user's own file
                _cleanup(p)

    with tempfile.TemporaryDirectory() as tmp:
        out_path = os.path.join(tmp, "index.scip")
        argv = [*cmd, "index", "--output", out_path, "--no-progress-bar", "--infer-tsconfig"]
        try:
            proc = subprocess.run(
                argv, cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, timeout=_SCIP_TIMEOUT_SECONDS, check=False,
            )
        except OSError as exc:  # FileNotFoundError, PermissionError (non-executable override), etc.
            _cleanup_artifacts()
            raise ScipTypescriptUnavailable(f"could not execute {argv[0]!r}: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            _cleanup_artifacts()
            raise ScipTypescriptUnavailable(f"scip-typescript timed out after {_SCIP_TIMEOUT_SECONDS}s") from exc
        # Our output is the explicit --output path. Only fall back to a root/index.scip that WE created
        # (an old scip-typescript ignoring --output) -- never a pre-existing one the user put there.
        if os.path.exists(out_path):
            produced = out_path
        elif os.path.exists(stray) and stray not in pre_existing:
            produced = stray
        else:
            produced = None
        try:
            if proc.returncode != 0 or produced is None or os.path.getsize(produced) == 0:
                tail = (proc.stderr or b"").decode("utf-8", "replace")[-600:]
                raise ScipTypescriptUnavailable(f"scip-typescript index exited {proc.returncode}: {tail}")
            with open(produced, "rb") as fh:
                data = fh.read()
        finally:
            _cleanup_artifacts()
    return data


def _cleanup(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass  # best-effort -- a leftover artifact in a temp checkout is harmless; in a live dir we try
