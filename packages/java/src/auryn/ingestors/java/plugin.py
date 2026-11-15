"""Import-light Java language registration -- the `auryn.languages` entry-point target.

Importing this module registers the Java GitLanguage without importing tree-sitter: the
GitLanguage's `sync` is a lazy thunk that imports auryn.ingestors.java.git_sync only when a
source actually contains `.java` files. Single-phase today (fast tree-sitter-java); a deep
pass -- Eclipse JDT / javac symbol resolution for resolved types, cross-file overload
selection, and inherited-member calls -- would slot in here as `deep_sync` (making Java
two-phase like Python). Suffix/exclude constants live here (single source of truth); git_sync
imports them back."""

from __future__ import annotations

from auryn.core.registry import GitLanguage, register_git_language

LANGUAGE = "java"
FILE_SUFFIXES = (".java",)
EXCLUDED_DIR_NAMES = frozenset(
    {".git", "target", "build", "out", "bin", ".gradle", ".idea", "node_modules"}
)


def _sync(*args, **kwargs):
    """Deferred until a source has Java files -- imports the tree-sitter-java engine."""
    from auryn.ingestors.java.git_sync import _git_language_sync

    return _git_language_sync(*args, **kwargs)


register_git_language(
    GitLanguage(
        language=LANGUAGE,
        file_suffixes=FILE_SUFFIXES,
        excluded_dir_names=EXCLUDED_DIR_NAMES,
        sync=_sync,
    )
)
