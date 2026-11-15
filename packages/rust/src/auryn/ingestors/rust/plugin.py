"""Import-light Rust language registration -- the `auryn.languages` entry-point target.

Importing this module registers the Rust GitLanguage without importing tree-sitter: the
GitLanguage's `sync` is a lazy thunk that imports auryn.ingestors.rust.git_sync only when a
source actually contains `.rs` files. Single-phase today (fast tree-sitter-rust); a deep pass
-- rust-analyzer / rustc HIR symbol resolution for resolved types, cross-module import (`use`)
path resolution, trait-method dispatch, and generic monomorphization -- would slot in here as
`deep_sync` (making Rust two-phase like Python). Suffix/exclude constants live here (single
source of truth); git_sync imports them back."""

from __future__ import annotations

from auryn.core.registry import GitLanguage, register_git_language

LANGUAGE = "rust"
FILE_SUFFIXES = (".rs",)
EXCLUDED_DIR_NAMES = frozenset({".git", "target", "node_modules"})


def _sync(*args, **kwargs):
    """Deferred until a source has Rust files -- imports the tree-sitter-rust engine."""
    from auryn.ingestors.rust.git_sync import _git_language_sync

    return _git_language_sync(*args, **kwargs)


register_git_language(
    GitLanguage(
        language=LANGUAGE,
        file_suffixes=FILE_SUFFIXES,
        excluded_dir_names=EXCLUDED_DIR_NAMES,
        sync=_sync,
    )
)
