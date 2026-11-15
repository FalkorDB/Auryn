"""Import-light TypeScript/JavaScript language registration -- the `auryn.languages`
entry-point target.

Importing this module registers the `typescript` GitLanguage without importing tree-sitter:
the GitLanguage's `sync` is a lazy thunk that imports auryn.ingestors.typescript.git_sync
only when a source actually contains JS/TS files.

One language owns the whole JS/TS family. TS is a syntactic superset of JS, so a single
extractor handles both (JS just yields no type annotations), and -- crucially -- resolution
is global: a `.ts` file importing a `.js` module must resolve within one language's pass, so
splitting TS and JS into two GitLanguages would sever exactly the import/call edges we care
about. The TS vs JS distinction is preserved instead as a `dialect` property on Module nodes
(see grammar.py / schema.py). Single-phase today (fast tree-sitter); a deep tsserver /
TS-compiler pass -- resolved types, cross-module calls -- would slot in here as `deep_sync`
(making TS two-phase like Python). Suffix/exclude constants live here (single source of
truth); git_sync imports them back."""

from __future__ import annotations

from auryn.core.registry import GitLanguage, register_git_language

LANGUAGE = "typescript"
# The whole JS/TS family in one language. `.d.ts` ends with `.ts`, so it is covered here and
# flagged (is_declaration) at parse time rather than by a separate suffix.
FILE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
EXCLUDED_DIR_NAMES = frozenset(
    {
        ".git",
        "node_modules",
        "dist",
        "build",
        "out",
        "coverage",
        ".next",
        ".nuxt",
        ".svelte-kit",
        "vendor",
    }
)


def _sync(*args, **kwargs):
    """Deferred until a source has JS/TS files -- imports the tree-sitter engine."""
    from auryn.ingestors.typescript.git_sync import _git_language_sync

    return _git_language_sync(*args, **kwargs)


register_git_language(
    GitLanguage(
        language=LANGUAGE,
        file_suffixes=FILE_SUFFIXES,
        excluded_dir_names=EXCLUDED_DIR_NAMES,
        sync=_sync,
    )
)
