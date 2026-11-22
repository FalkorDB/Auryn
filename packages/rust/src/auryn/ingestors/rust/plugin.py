"""Import-light Rust language registration -- the `auryn.languages` entry-point target.

Importing this module registers the Rust GitLanguage without importing tree-sitter: the
GitLanguage's `sync`/`deep_sync` are lazy thunks that import auryn.ingestors.rust.git_sync only when
a source actually contains `.rs` files. Two-phase (like Python/C#): the fast `sync` is tree-sitter-rust
(by name); the deep `deep_sync` resolves CALLS precisely via rust-analyzer's SCIP index (trait dispatch,
generics, cross-module/crate), degrading to the fast pass when rust-analyzer or a buildable crate is
absent. Suffix/exclude constants live here (single source of truth); git_sync imports them back."""

from __future__ import annotations

from auryn.core.registry import GitLanguage, register_git_language

LANGUAGE = "rust"
FILE_SUFFIXES = (".rs",)
EXCLUDED_DIR_NAMES = frozenset({".git", "target", "node_modules"})


def _sync(*args, **kwargs):
    """Fast phase, deferred until a source has Rust files -- imports the tree-sitter-rust engine."""
    from auryn.ingestors.rust.git_sync import treesitter_language_sync

    return treesitter_language_sync(*args, **kwargs)


def _deep_sync(*args, **kwargs):
    """Deep phase (rust-analyzer / SCIP), deferred likewise -- degrades to the fast pass if unavailable."""
    from auryn.ingestors.rust.git_sync import rust_analyzer_language_sync

    return rust_analyzer_language_sync(*args, **kwargs)


register_git_language(
    GitLanguage(
        language=LANGUAGE,
        file_suffixes=FILE_SUFFIXES,
        excluded_dir_names=EXCLUDED_DIR_NAMES,
        sync=_sync,
        deep_sync=_deep_sync,
    )
)
