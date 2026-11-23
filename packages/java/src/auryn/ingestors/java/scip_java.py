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
import tempfile

from auryn.core import scip_runner

_SCIP_TIMEOUT_SECONDS = int(os.environ.get("AURYN_SCIP_JAVA_TIMEOUT", "1800"))  # a build can be slow

_INSTALL_HINT = (
    "deep Java analysis is OFF: set AURYN_SCIP_JAVA to a scip-java launcher to opt in. It is not "
    "auto-discovered on PATH because `scip-java index` runs the project's build (executing the repo's "
    "build scripts/plugins). Install e.g. `cs bootstrap --standalone -o scip-java "
    "-M org.scip_code.scip_java.ScipJava org.scip-code:scip-java:latest.release`; needs a JDK (17+) "
    "and the project's build tool."
)

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


class ScipJavaUnavailable(scip_runner.ScipIndexerUnavailable):
    """scip-java could not be located or failed to produce a SCIP index (not opted in, missing JDK/build
    tool, or a build failure) -- the signal for git_sync to fall back to the tree-sitter fast pass."""


def _tool_command() -> list[str]:
    """Locate scip-java -- ONLY via an explicit `AURYN_SCIP_JAVA` launcher path, deliberately NOT from
    PATH (which=None). Unlike the parse-only indexers (scip-typescript, scip-go), `scip-java index` RUNS THE
    PROJECT'S BUILD (Maven/Gradle/Bazel), executing the repository's own build scripts, plugins, and
    annotation processors -- i.e. arbitrary code from the (possibly untrusted) indexed repo. Since deep runs
    automatically (async by default), auto-discovering on PATH would silently execute untrusted builds, so
    the explicit env var makes it a deliberate per-environment OPT-IN. See scip_runner.resolve_trusted_tool."""
    return scip_runner.resolve_trusted_tool(
        env_var="AURYN_SCIP_JAVA", which=None, unavailable=ScipJavaUnavailable, hint=_INSTALL_HINT)


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
    # Refuse a launcher inside the ORIGINAL (untrusted) tree -- checked against `root`, not the build copy
    # we run in below (the copy would contain a repo-shipped binary too).
    if scip_runner.is_inside(cmd[0], root):
        raise ScipJavaUnavailable(f"refusing to run scip-java from inside the indexed tree: {cmd[0]}")
    with tempfile.TemporaryDirectory() as tmp:
        build_dir = os.path.join(tmp, "proj")
        try:
            shutil.copytree(root, build_dir, ignore=_copy_ignore(root), symlinks=True)
        except (OSError, shutil.Error) as exc:
            raise ScipJavaUnavailable(f"could not stage a build copy of {root!r}: {exc}") from exc
        out_path = os.path.join(tmp, "index.scip")
        argv = [*cmd, "index", f"--output={out_path}"]
        return scip_runner.run_indexer(argv, cwd=build_dir, out_path=out_path, timeout=_SCIP_TIMEOUT_SECONDS,
                                       tool="scip-java", unavailable=ScipJavaUnavailable)
