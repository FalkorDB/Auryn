"""Import-light Kotlin language registration -- the `auryn.languages` entry-point target.

Importing this module registers the Kotlin GitLanguage without importing tree-sitter: the
GitLanguage's `sync` is a lazy thunk that imports auryn.ingestors.kotlin.git_sync only when a
source actually contains `.kt`/`.kts` files. Single-phase today (fast tree-sitter-kotlin); a deep
pass -- the Kotlin compiler / kotlin-language-server for resolved types, cross-file overload
selection, extension-receiver dispatch, and inherited-member calls -- would slot in here as
`deep_sync` (making Kotlin two-phase like Python). Modelled as Java's sibling (JVM, packages,
classes/interfaces, single-superclass + interface-list inheritance). Suffix/exclude constants live
here (single source of truth); git_sync imports them back."""

from __future__ import annotations

from auryn.core.registry import GitLanguage, register_git_language

LANGUAGE = "kotlin"
FILE_SUFFIXES = (".kt", ".kts")
EXCLUDED_DIR_NAMES = frozenset(
    {".git", "build", ".gradle", "out", "bin", ".idea", "node_modules"}
)


def _sync(*args, **kwargs):
    """Deferred until a source has Kotlin files -- imports the tree-sitter-kotlin engine."""
    from auryn.ingestors.kotlin.git_sync import _git_language_sync

    return _git_language_sync(*args, **kwargs)


register_git_language(
    GitLanguage(
        language=LANGUAGE,
        file_suffixes=FILE_SUFFIXES,
        excluded_dir_names=EXCLUDED_DIR_NAMES,
        sync=_sync,
    )
)
