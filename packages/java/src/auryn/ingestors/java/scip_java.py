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

# Build-output dirs to skip when staging a build sandbox -- but ONLY where they're build output (beside a
# project/subproject `src/`), NEVER a same-named SOURCE package (`src/main/java/com/acme/build/`). `.git`
# is skipped at any level (root repo or a submodule). See _copy_ignore.
_ARTIFACT_DIRS = frozenset({"target", "build", ".gradle", "out", "bin", ".idea", ".vscode", "node_modules"})
_VCS_DIRS = frozenset({".git"})


def _copy_ignore(root: str):
    """A copytree `ignore` callable that drops build-output dirs only OUTSIDE a `src/` tree (build output
    sits beside src/, at the root or a subproject root -- never inside it), so a legitimate source package
    named `build`/`target`/etc. is preserved. `.git` is dropped anywhere."""
    def ignore(dirpath: str, names: list[str]) -> set[str]:
        drop = {n for n in names if n in _VCS_DIRS}
        rel_parts = set(os.path.relpath(dirpath, root).split(os.sep))
        if "src" not in rel_parts:  # not inside a source tree -> a `build`/`target` here is build output
            drop |= {n for n in names if n in _ARTIFACT_DIRS}
        return drop
    return ignore


class ScipJavaUnavailable(RuntimeError):
    """scip-java could not be located or failed to produce a SCIP index (not opted in, missing JDK/build
    tool, or a build failure) -- the signal for git_sync to fall back to the tree-sitter fast pass."""


def _tool_command() -> list[str]:
    """Locate scip-java -- ONLY via an explicit `AURYN_SCIP_JAVA` launcher path, deliberately NOT from
    PATH. Unlike the parse-only indexers (scip-typescript, scip-go), `scip-java index` RUNS THE PROJECT'S
    BUILD (Maven/Gradle/Bazel), executing the repository's own build scripts, plugins, and annotation
    processors -- i.e. arbitrary code from the (possibly untrusted) indexed repo, with the ingest process's
    filesystem access and credentials. Since deep runs automatically (async by default), auto-discovering
    scip-java on PATH would silently execute untrusted builds; requiring the explicit env var makes running
    the build a deliberate per-environment OPT-IN. The launcher must be an absolute realpath (never re-
    resolved against the subprocess cwd); run_scip further refuses one inside the indexed tree."""
    override = os.environ.get("AURYN_SCIP_JAVA")
    if override:
        resolved = os.path.realpath(override)
        if os.path.isfile(resolved):
            return [resolved]
    raise ScipJavaUnavailable(
        "deep Java analysis is OFF: set AURYN_SCIP_JAVA to a scip-java launcher to opt in. It is not "
        "auto-discovered on PATH because `scip-java index` runs the project's build (executing the repo's "
        "build scripts/plugins). Install e.g. `cs bootstrap --standalone -o scip-java "
        "-M org.scip_code.scip_java.ScipJava org.scip-code:scip-java:latest.release`; needs a JDK (17+) "
        "and the project's build tool."
    )


def _is_inside(path: str, root: str) -> bool:
    root_real = os.path.realpath(root)
    try:
        return os.path.commonpath([os.path.realpath(path), root_real]) == root_real
    except ValueError:  # different drives (Windows) -> not inside
        return False


def run_scip(root: str) -> bytes:
    """Run `scip-java index` over the project at `root` and return the raw `index.scip` bytes. Raises
    ScipJavaUnavailable on any failure (not opted in, build failure, timeout, no output) so the caller can
    fall back to the fast pass. `root` must be a project directory (a pom.xml / build.gradle) on disk.

    The build is run in a THROWAWAY COPY of the project, never in `root` itself: `scip-java index` runs the
    build tool (often `clean verify`), which writes -- and deletes -- build output dirs. For a git source
    `root` is already a disposable checkout, but for a LOCAL-directory source it is the user's live tree,
    and ADR 0001 forbids writing there; staging a copy honors that uniformly. The copy excludes build
    output / VCS / deps (the build regenerates what it needs, and Maven/Gradle deps live in the shared
    ~/.m2 or ~/.gradle). scip-java auto-detects the build tool and hard-fails with no index when the build
    fails, so success is gated on a non-empty index file. The subprocess inherits the environment
    (JAVA_HOME / PATH for the JDK + build tool)."""
    cmd = _tool_command()
    if _is_inside(cmd[0], root):  # never run a scip-java that lives inside the (untrusted) indexed tree
        raise ScipJavaUnavailable(f"refusing to run scip-java from inside the indexed tree: {cmd[0]}")
    with tempfile.TemporaryDirectory() as tmp:
        build_dir = os.path.join(tmp, "proj")
        try:
            shutil.copytree(root, build_dir, ignore=_copy_ignore(root), symlinks=True)
        except (OSError, shutil.Error) as exc:
            raise ScipJavaUnavailable(f"could not stage a build copy of {root!r}: {exc}") from exc
        out_path = os.path.join(tmp, "index.scip")
        argv = [*cmd, "index", f"--output={out_path}"]
        try:
            proc = subprocess.run(
                argv, cwd=build_dir, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
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
