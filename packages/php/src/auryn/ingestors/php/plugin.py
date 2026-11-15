"""Import-light PHP language registration -- the `auryn.languages` entry-point target.

Importing this module registers the PHP GitLanguage without importing tree-sitter: the
GitLanguage's `sync` is a lazy thunk that imports auryn.ingestors.php.git_sync only when a
source actually contains `.php` files. Single-phase today (fast tree-sitter-php); a deep pass --
full PHP name-resolution (use-statement aliasing, autoloading/PSR-4, inherited-member calls,
trait-flattened method sets) for precise cross-file symbol binding -- would slot in here as
`deep_sync` (making PHP two-phase like Python). Suffix/exclude constants live here (single source
of truth); git_sync imports them back."""

from __future__ import annotations

from auryn.core.registry import GitLanguage, register_git_language

LANGUAGE = "php"
FILE_SUFFIXES = (".php",)
EXCLUDED_DIR_NAMES = frozenset(
    {".git", "vendor", "node_modules", "build", "dist", "cache", ".cache", "storage", ".idea"}
)


def _sync(*args, **kwargs):
    """Deferred until a source has PHP files -- imports the tree-sitter-php engine."""
    from auryn.ingestors.php.git_sync import _git_language_sync

    return _git_language_sync(*args, **kwargs)


register_git_language(
    GitLanguage(
        language=LANGUAGE,
        file_suffixes=FILE_SUFFIXES,
        excluded_dir_names=EXCLUDED_DIR_NAMES,
        sync=_sync,
    )
)
