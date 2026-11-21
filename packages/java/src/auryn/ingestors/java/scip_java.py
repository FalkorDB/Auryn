"""Deep Java call resolution via Sourcegraph's scip-java indexer (the `deep_sync` engine, mirroring the
Rust/Go/TS SCIP passes and C#'s Roslyn / Python's jedi).

The fast tree-sitter pass resolves CALLS *by name* and drops what it can't name (overloads, inherited /
interface dispatch, cross-package callees that collide with a local). scip-java resolves them precisely
via the compiler. We ask it for a SCIP index (`scip-java index`); the index is turned into resolved CALLS
by the shared, language-agnostic `core.scip_resolve` (occurrence containment over the fast pass's
`CallableLocation`s) -- the same code the Rust/Go/TS deep passes use. This module is just the Java-specific
RUNNER: locate scip-java and produce the `index.scip` bytes.

Graceful degradation is the contract: if scip-java isn't available or the project doesn't build, a
`ScipJavaUnavailable` is raised and git_sync falls back to the tree-sitter fast pass. This matters more
than for the other languages: scip-java RUNS THE PROJECT'S BUILD (Maven/Gradle/Bazel, JDK 17+) and
HARD-fails with no index when the build fails, so deep is opportunistic -- it no-ops on any repo that
doesn't build cleanly in this environment. NOTE: running the build writes the build tool's output dirs
(target/ , build/) into the indexed directory; for a git source that's a throwaway checkout, but for a
LOCAL-directory source it is the user's live tree (those dirs are in the Java ingestor's excluded set, so
a subsequent fast ingest ignores them). v1 resolves INTRA-project calls only and leaves extern://
placeholders untouched."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

_SCIP_TIMEOUT_SECONDS = int(os.environ.get("AURYN_SCIP_JAVA_TIMEOUT", "1800"))  # a build can be slow


class ScipJavaUnavailable(RuntimeError):
    """scip-java could not be located or failed to produce a SCIP index (missing tool, no JDK/build tool,
    or a build failure) -- the signal for git_sync to fall back to the tree-sitter fast pass."""


def _tool_command() -> list[str]:
    """Locate a usable scip-java from a TRUSTED location ONLY:
      1. `AURYN_SCIP_JAVA` env override (an explicit, user-trusted launcher path -- a coursier bootstrap
         or the Docker wrapper);
      2. a `scip-java` on PATH.
    Returned as an ABSOLUTE realpath (symlinks resolved) so the subprocess never re-resolves it against
    its cwd and run_scip can refuse a binary inside the indexed tree. Raises ScipJavaUnavailable if no
    trusted launcher resolves."""
    candidate = os.environ.get("AURYN_SCIP_JAVA") or shutil.which("scip-java")
    if candidate:
        resolved = os.path.realpath(candidate)
        if os.path.isfile(resolved):
            return [resolved]
    raise ScipJavaUnavailable(
        "scip-java not found on PATH. Install it (e.g. `cs bootstrap --standalone -o scip-java "
        "-M org.scip_code.scip_java.ScipJava org.scip-code:scip-java:latest.release`) or set "
        "AURYN_SCIP_JAVA to its launcher path. Needs a JDK (17+) and the project's build tool."
    )


def _is_inside(path: str, root: str) -> bool:
    root_real = os.path.realpath(root)
    try:
        return os.path.commonpath([os.path.realpath(path), root_real]) == root_real
    except ValueError:  # different drives (Windows) -> not inside
        return False


def run_scip(root: str) -> bytes:
    """Run `scip-java index` over the project at `root` and return the raw `index.scip` bytes. Raises
    ScipJavaUnavailable on any failure (tool missing, build failure, timeout, no output) so the caller can
    fall back to the fast pass. `root` must be a project directory (a pom.xml / build.gradle) on disk.

    scip-java auto-detects the build tool and HARD-fails with no index when the build fails, so success is
    gated on a non-empty index file existing (the lenient-exit-code concern of the other runners doesn't
    apply, but we gate the same way for uniformity). `--output` is written to a tempdir, outside the
    indexed tree. The subprocess inherits the environment (JAVA_HOME / PATH for the JDK + build tool)."""
    cmd = _tool_command()
    if _is_inside(cmd[0], root):  # never run a scip-java that lives inside the (untrusted) indexed tree
        raise ScipJavaUnavailable(f"refusing to run scip-java from inside the indexed tree: {cmd[0]}")
    with tempfile.TemporaryDirectory() as tmp:
        out_path = os.path.join(tmp, "index.scip")
        argv = [*cmd, "index", f"--output={out_path}"]
        try:
            proc = subprocess.run(
                argv, cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, timeout=_SCIP_TIMEOUT_SECONDS, check=False,
            )
        except OSError as exc:  # FileNotFoundError, PermissionError (non-executable override), etc.
            raise ScipJavaUnavailable(f"could not execute {argv[0]!r}: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ScipJavaUnavailable(f"scip-java timed out after {_SCIP_TIMEOUT_SECONDS}s") from exc
        if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            tail = (proc.stderr or b"").decode("utf-8", "replace")[-600:]
            raise ScipJavaUnavailable(f"scip-java index produced no output (exit {proc.returncode}): {tail}")
        with open(out_path, "rb") as fh:
            return fh.read()
