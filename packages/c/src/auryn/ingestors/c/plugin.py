"""Import-light C language registration -- the `auryn.languages` entry-point target.

Importing this module registers the C GitLanguage without importing tree-sitter: the
GitLanguage's `sync` is a lazy thunk that imports auryn.ingestors.c.git_sync only when a
source actually contains C files. Single-phase today (fast tree-sitter-c); a deep libclang
pass -- resolved types, header<->.c linking, cross-TU calls, needing a compile_commands.json
-- would slot in here as `deep_sync` (making C two-phase like Python). Suffix/exclude
constants live here (single source of truth); git_sync imports them back."""

from __future__ import annotations

from auryn.core.registry import GitLanguage, register_git_language

LANGUAGE = "c"
FILE_SUFFIXES = (".c", ".h")
EXCLUDED_DIR_NAMES = frozenset(
    # Kept identical to the C++ ingestor's set so the shared `.h` ownership decision agrees: a C++-content
    # `.h` in a dir one engine excludes but the other walks would be disowned by C yet never claimed by C++
    # (orphaned). `out` is a build-output dir both should skip.
    {".git", "build", "cmake-build-debug", "node_modules", "vendor", "third_party", "external", "out"}
)


def _sync(*args, **kwargs):
    """Deferred until a source has C files -- imports the tree-sitter-c engine."""
    from auryn.ingestors.c.git_sync import _git_language_sync

    return _git_language_sync(*args, **kwargs)


register_git_language(
    GitLanguage(
        language=LANGUAGE,
        file_suffixes=FILE_SUFFIXES,
        excluded_dir_names=EXCLUDED_DIR_NAMES,
        sync=_sync,
    )
)
