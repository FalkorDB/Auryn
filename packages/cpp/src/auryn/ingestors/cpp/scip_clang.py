"""Deep C++ call resolution via Sourcegraph's scip-clang indexer (the `deep_sync` engine, mirroring the
Rust/Go/TS/Java SCIP passes).

The fast tree-sitter pass resolves CALLS *by name*, so an overload, a virtual/interface call, or a
cross-file callee is dropped or guessed. scip-clang resolves them precisely via Clang. We ask it for a
SCIP index (`scip-clang --compdb-path=...`); the index is turned into resolved CALLS by the shared,
language-agnostic `core.scip_resolve` (occurrence containment over the fast pass's `CallableLocation`s) --
the same code the other deep passes use. This module is just the C++-specific RUNNER: locate scip-clang,
find a compilation database, and produce the `index.scip` bytes.

scip-clang needs a `compile_commands.json` (a compilation database -- CMake's
`-DCMAKE_EXPORT_COMPILE_COMMANDS=ON`, Bear, etc.), so deep is opportunistic: it falls back to the fast
pass when none is discoverable. Deep C++ is an explicit OPT-IN (`AURYN_SCIP_CLANG`, not PATH): a repo's
compile_commands.json carries the compiler flags scip-clang runs Clang with, which can include
`-fplugin=...` / response files -- i.e. a path to arbitrary code execution from an untrusted repo. The
compdb's entries use ABSOLUTE `directory` paths, so in practice this works on a LOCAL-directory source
(the live tree where the build generated the compdb); scip-clang reads the sources read-only and writes
only the index to our temp `--index-output-path` (it does not write into the source tree). v1 resolves
INTRA-project calls only; one integration can later serve C and CUDA (shared engine)."""

from __future__ import annotations

import os
import subprocess
import tempfile

_SCIP_TIMEOUT_SECONDS = int(os.environ.get("AURYN_SCIP_CLANG_TIMEOUT", "1800"))


class ScipClangUnavailable(RuntimeError):
    """scip-clang could not be located, no compilation database was found, or indexing failed -- the
    signal for git_sync to fall back to the tree-sitter fast pass."""


def _tool_command() -> list[str]:
    """Locate scip-clang -- ONLY via an explicit `AURYN_SCIP_CLANG` path, deliberately NOT from PATH.
    `scip-clang` runs Clang per the repo's `compile_commands.json`, whose flags (`-fplugin=...`, `@response`
    files, include paths) are attacker-controllable in an untrusted repo and can execute arbitrary code, so
    deep C++ is a deliberate per-environment OPT-IN. Returned as an absolute realpath (never re-resolved
    against the subprocess cwd); run_scip further refuses a binary inside the indexed tree."""
    override = os.environ.get("AURYN_SCIP_CLANG")
    if override:
        resolved = os.path.realpath(override)
        if os.path.isfile(resolved):
            return [resolved]
    raise ScipClangUnavailable(
        "deep C++ analysis is OFF: set AURYN_SCIP_CLANG to a scip-clang binary to opt in. It is not "
        "auto-discovered on PATH because it runs Clang with the repo's compile_commands.json flags "
        "(which can execute arbitrary code). Download it from github.com/sourcegraph/scip-clang/releases."
    )


def _find_compdb(root: str) -> str | None:
    """Discover a compilation database for `root`: `AURYN_CPP_COMPDB` (explicit), else a
    `compile_commands.json` at the root or a conventional `build/` dir. Returns an absolute path or None."""
    override = os.environ.get("AURYN_CPP_COMPDB")
    if override and os.path.isfile(override):
        return os.path.abspath(override)
    for rel in ("compile_commands.json", os.path.join("build", "compile_commands.json")):
        cand = os.path.join(root, rel)
        if os.path.isfile(cand):
            return os.path.abspath(cand)
    return None


def _is_inside(path: str, root: str) -> bool:
    root_real = os.path.realpath(root)
    try:
        return os.path.commonpath([os.path.realpath(path), root_real]) == root_real
    except ValueError:  # different drives (Windows) -> not inside
        return False


def run_scip(root: str) -> bytes:
    """Run scip-clang over the project at `root` and return the raw `index.scip` bytes. Raises
    ScipClangUnavailable on any failure (not opted in, no compile_commands.json, non-zero exit, no output)
    so the caller can fall back to the fast pass. The index is written to a tempdir, never into `root`;
    scip-clang reads the sources read-only (it produces no object files)."""
    cmd = _tool_command()
    if _is_inside(cmd[0], root):  # never run a scip-clang that lives inside the (untrusted) indexed tree
        raise ScipClangUnavailable(f"refusing to run scip-clang from inside the indexed tree: {cmd[0]}")
    compdb = _find_compdb(root)
    if compdb is None:
        raise ScipClangUnavailable(
            "no compile_commands.json found (at the project root or build/); generate one "
            "(CMake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON, or Bear) or set AURYN_CPP_COMPDB."
        )
    with tempfile.TemporaryDirectory() as tmp:
        out_path = os.path.join(tmp, "index.scip")
        argv = [*cmd, f"--compdb-path={compdb}", f"--index-output-path={out_path}"]
        try:
            proc = subprocess.run(
                argv, cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, timeout=_SCIP_TIMEOUT_SECONDS, check=False,
            )
        except OSError as exc:  # FileNotFoundError, PermissionError (non-executable override), etc.
            raise ScipClangUnavailable(f"could not execute {argv[0]!r}: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ScipClangUnavailable(f"scip-clang timed out after {_SCIP_TIMEOUT_SECONDS}s") from exc
        if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            tail = (proc.stderr or b"").decode("utf-8", "replace")[-600:]
            raise ScipClangUnavailable(f"scip-clang produced no index (exit {proc.returncode}): {tail}")
        with open(out_path, "rb") as fh:
            return fh.read()
