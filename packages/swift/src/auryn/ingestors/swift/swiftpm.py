"""SwiftPM module awareness for the Swift ingestor.

A Swift *module* is the compilation + namespace unit: a type `Foo` in module `A` is `A.Foo` to a
consumer, and `A.Foo` and `B.Foo` are DIFFERENT types. SwiftPM lays a module out as one directory
of sources -- `Sources/<Target>/**.swift` (library/executable targets) or `Tests/<Target>/**.swift`
(test targets) -- so the module a file belongs to is derivable FROM ITS PATH ALONE, no manifest
parse. Before this, the ingestor hung every top-level type/function off the project, so two targets
that both declared `Foo` collapsed onto one node.

This mirrors the Rust crate / Go package work: the module is the per-file scope root, keyed by a
pure-path rule. (Package.swift also declares the targets and their dependencies, but it is Swift
code, not a declarative manifest; the standard `Sources/<Target>/` layout gives the target name
without parsing it, and inter-target `import`s already connect targets via ADR-0004 healing.)
"""

from __future__ import annotations

import re

# The two directories under which SwiftPM nests one subdirectory per target.
_TARGET_ROOTS = ("Sources", "Tests")

# SwiftPM derives a module name from the target by replacing every character that is not valid in a
# C99 identifier with `_` (so `Sources/My-Lib/` is imported as `My_Lib`). The module name -- used in
# ids and in the cross-repo linkage keys -- must match what `import` writes, or healing misses.
_NON_IDENT = re.compile(r"[^A-Za-z0-9_]")


def _c99(name: str) -> str:
    return _NON_IDENT.sub("_", name)


def _norm(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def module_for_path(path: str, package_name: str) -> str:
    """The SwiftPM module (target) a `.swift` file belongs to, from its path:

        Sources/<Target>/**.swift  -> <Target>          (a library/executable target)
        Tests/<Target>/**.swift    -> <Target>          (a test target, e.g. FooTests)
        Sources/**.swift (flat)    -> <package_name>    (SwiftPM's single implicit target)
        anything else (root file, Package.swift, …)     -> <package_name>

    `package_name` is the fallback single-module name (the project name -- the SwiftPM package name
    is declared in Package.swift, which this fast pass does not parse). The target name is kept
    CASE-PRESERVED: it is exactly what an `import <Target>` writes, so a cross-repo import heals onto
    the defining target (ADR 0004)."""
    parts = _norm(path).split("/")
    for i, seg in enumerate(parts[:-1]):
        if seg in _TARGET_ROOTS:
            rest = parts[i + 1:]
            # Sources/<Target>/<file-or-more> -> the target subdirectory; Sources/<file>.swift
            # (no subdirectory) is SwiftPM's flat single-target layout -> the package itself.
            if len(rest) >= 2:
                return _c99(rest[0])
            return _c99(package_name)
    return _c99(package_name)
