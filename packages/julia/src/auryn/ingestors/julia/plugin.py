"""Import-light Julia language registration -- the `auryn.languages` entry-point target.

Importing this module registers the Julia GitLanguage without importing tree-sitter: the
GitLanguage's `sync` is a lazy thunk that imports auryn.ingestors.julia.git_sync only when a
source actually contains `.jl` files. Single-phase today (fast tree-sitter-julia); a deep pass
-- Julia's own type inference / lowering (`Base.code_typed`, `methods`, `which`) for precise
multiple-dispatch resolution (which concrete method a call actually dispatches to), resolved
supertype chains, and inferred parameter/return types -- would slot in here as `deep_sync`
(making Julia two-phase like Python). Suffix/exclude constants live here (single source of
truth); git_sync imports them back."""

from __future__ import annotations

from auryn.core.registry import GitLanguage, register_git_language

LANGUAGE = "julia"
FILE_SUFFIXES = (".jl",)
EXCLUDED_DIR_NAMES = frozenset(
    {".git", "deps", "node_modules", "build", "dist", ".julia"}
)


def _sync(*args, **kwargs):
    """Deferred until a source has Julia files -- imports the tree-sitter-julia engine."""
    from auryn.ingestors.julia.git_sync import _git_language_sync

    return _git_language_sync(*args, **kwargs)


register_git_language(
    GitLanguage(
        language=LANGUAGE,
        file_suffixes=FILE_SUFFIXES,
        excluded_dir_names=EXCLUDED_DIR_NAMES,
        sync=_sync,
    )
)
