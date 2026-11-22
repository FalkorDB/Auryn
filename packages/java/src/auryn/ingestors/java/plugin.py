"""Import-light Java language registration -- the `auryn.languages` entry-point target.

Importing this module registers the Java GitLanguage without importing tree-sitter: the
GitLanguage's `sync`/`deep_sync` are lazy thunks that import auryn.ingestors.java.git_sync only
when a source actually contains `.java` files. Two-phase (like Python/C#/Rust/TS/Go): the fast
`sync` is tree-sitter-java (CALLS by name; drops what it can't name); the deep `deep_sync`
resolves CALLS precisely via scip-java's SCIP index (overload selection, inherited/interface
dispatch, cross-package callees), degrading to the fast pass when scip-java, a JDK/build tool, or
a buildable project is absent -- scip-java RUNS THE PROJECT'S BUILD, so deep is opportunistic.
Suffix/exclude constants live here (single source of truth); git_sync imports them back (the build
output dirs target/build/.gradle are already excluded, so scip-java's build never pollutes a
subsequent fast ingest)."""

from __future__ import annotations

from auryn.core.registry import GitLanguage, register_git_language

LANGUAGE = "java"
FILE_SUFFIXES = (".java",)
EXCLUDED_DIR_NAMES = frozenset(
    {".git", "target", "build", "out", "bin", ".gradle", ".idea", "node_modules"}
)


def _sync(*args, **kwargs):
    """Fast phase, deferred until a source has Java files -- imports the tree-sitter-java engine."""
    from auryn.ingestors.java.git_sync import treesitter_language_sync

    return treesitter_language_sync(*args, **kwargs)


def _deep_sync(*args, **kwargs):
    """Deep phase (scip-java / SCIP), deferred likewise -- degrades to the fast pass if unavailable."""
    from auryn.ingestors.java.git_sync import scip_java_language_sync

    return scip_java_language_sync(*args, **kwargs)


register_git_language(
    GitLanguage(
        language=LANGUAGE,
        file_suffixes=FILE_SUFFIXES,
        excluded_dir_names=EXCLUDED_DIR_NAMES,
        sync=_sync,
        deep_sync=_deep_sync,
    )
)
