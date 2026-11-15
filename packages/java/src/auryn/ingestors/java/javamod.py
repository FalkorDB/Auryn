"""Maven/Gradle module awareness for the Java ingestor.

A Maven/Gradle *module* (subproject) is a build + (with JPMS) a namespacing unit: within one build
only that module's own packages are on the classpath, so two modules that each declare package
`com.acme.util` with a class `Helper` are DIFFERENT types. The standard Maven/Gradle layout nests a
module's sources under `<module>/src/{main,test}/{java,kotlin}/<package-path>/…`, so the module a
file belongs to is derivable FROM ITS PATH ALONE -- the directory holding that `src/<set>/<lang>`
marker -- with no pom.xml/build.gradle parse. Before this, the ingestor keyed every package/type by
its dotted package under the PROJECT, so two modules' `com.acme.util.Helper` collapsed onto one node.

This mirrors the Rust crate / Go package / Swift target work: the module is the per-file scope root
over the package tier, keyed by a pure-path rule. One difference from Go/Swift: a consumer imports a
Java type by its fully-qualified *package* name, never the Gradle module name, so the Java module is
purely INTERNAL structure -- it scopes node ids and biases resolution, but carries no cross-repo
linkage key and heals nothing. Cross-repo healing stays FQCN-based (ADR 0004), unchanged.
"""

from __future__ import annotations

# Maven/Gradle nest a module's sources under `src/{main,test}/{java,kotlin}`. Both language leaves
# are accepted so a module with mixed Java/Kotlin sources resolves to the SAME module dir whichever
# ingestor walks it.
_SRC_SETS = frozenset({"main", "test"})
_SRC_LANGS = frozenset({"java", "kotlin"})


def _norm(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def module_for_path(path: str) -> str:
    """The Maven/Gradle module (subproject) a source file belongs to, from its path:

        <module>/src/{main,test}/{java,kotlin}/**  -> "<module>"  (a subproject; may be nested: `libs/data`)
        src/{main,test}/{java,kotlin}/**           -> ""          (a root-level / single-module layout)
        anything else (loose file, no src layout)  -> ""          (root)

    A subproject is the repo-relative directory holding `src/…` -- stable, path-derived, unique --
    and is used verbatim as both the Module id segment and its display name. The empty string marks a
    ROOT-level file, which the caller scopes under the Project itself (NOT a Module named after the
    project): that keeps a root file and a subproject literally named after the project from colliding,
    and means the Module tier appears only when there genuinely are subprojects."""
    parts = _norm(path).split("/")
    for i in range(len(parts) - 3):  # room for src/<set>/<lang>/ + at least a filename after it
        if parts[i] == "src" and parts[i + 1] in _SRC_SETS and parts[i + 2] in _SRC_LANGS:
            return "/".join(parts[:i])  # "" when the marker is at the repo root
    return ""
