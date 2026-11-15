"""Import-light Ruby language registration -- the `auryn.languages` entry-point target.

Importing this module registers the Ruby GitLanguage without importing tree-sitter: the
GitLanguage's `sync` is a lazy thunk that imports auryn.ingestors.ruby.git_sync only when a
source actually contains `.rb` files. Single-phase today (fast tree-sitter-ruby); a deep pass --
a real Ruby loader / Sorbet / RBS-backed resolver for method dispatch, receiver types, mixin
ancestry, and cross-file constant resolution -- would slot in here as `deep_sync` (making Ruby
two-phase like Python). Ruby is a dynamic language (open classes, method_missing, metaprogramming)
so the fast pass is name-based and deliberately conservative: it emits an edge only when it can
name a target, never a guess (see resolution.py). Suffix/exclude constants live here (single
source of truth); git_sync imports them back."""

from __future__ import annotations

from auryn.core.registry import GitLanguage, register_git_language

LANGUAGE = "ruby"
FILE_SUFFIXES = (".rb",)
# VCS/editor plus the conventional Ruby non-source trees: bundled gems (`vendor`), transient
# `tmp`/`log`, and the JS toolchain's `node_modules` (common in Rails apps).
EXCLUDED_DIR_NAMES = frozenset({".git", "vendor", "node_modules", "tmp", "log"})


def _sync(*args, **kwargs):
    """Deferred until a source has Ruby files -- imports the tree-sitter-ruby engine."""
    from auryn.ingestors.ruby.git_sync import _git_language_sync

    return _git_language_sync(*args, **kwargs)


register_git_language(
    GitLanguage(
        language=LANGUAGE,
        file_suffixes=FILE_SUFFIXES,
        excluded_dir_names=EXCLUDED_DIR_NAMES,
        sync=_sync,
    )
)
