"""Import-light Swift language registration -- the `auryn.languages` entry-point target.

Importing this module registers the Swift GitLanguage without importing tree-sitter: the
GitLanguage's `sync` is a lazy thunk that imports auryn.ingestors.swift.git_sync only when a
source actually contains `.swift` files. Single-phase today (fast tree-sitter-swift); a deep
pass -- SourceKit-LSP / swiftc symbol resolution for resolved types, cross-module import
resolution, protocol-witness dispatch, and precise overload selection -- would slot in here as
`deep_sync` (making Swift two-phase like Python). Suffix/exclude constants live here (single
source of truth); git_sync imports them back."""

from __future__ import annotations

from auryn.core.registry import GitLanguage, register_git_language

LANGUAGE = "swift"
FILE_SUFFIXES = (".swift",)
EXCLUDED_DIR_NAMES = frozenset({".git", ".build", "Pods", "Carthage", "node_modules"})


def _sync(*args, **kwargs):
    """Deferred until a source has Swift files -- imports the tree-sitter-swift engine."""
    from auryn.ingestors.swift.git_sync import _git_language_sync

    return _git_language_sync(*args, **kwargs)


register_git_language(
    GitLanguage(
        language=LANGUAGE,
        file_suffixes=FILE_SUFFIXES,
        excluded_dir_names=EXCLUDED_DIR_NAMES,
        sync=_sync,
    )
)
