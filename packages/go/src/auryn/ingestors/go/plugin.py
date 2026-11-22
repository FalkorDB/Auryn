"""Import-light Go language registration -- the `auryn.languages` entry-point target.

Importing this module registers the Go GitLanguage without importing tree-sitter: the
GitLanguage's `sync`/`deep_sync` are lazy thunks that import auryn.ingestors.go.git_sync only when
a source actually contains `.go` files. Two-phase (like Python/C#/Rust/TS): the fast `sync` is
tree-sitter-go (CALLS by name); the deep `deep_sync` resolves CALLS precisely via scip-go's SCIP
index (go/types-backed: interface/receiver-typed dispatch, cross-package callees that collide with a
same-named local), degrading to the fast pass when scip-go or a loadable module is absent.

Structural **IMPLEMENTS** (Go interface satisfaction is implicit -- a type implements an interface
iff it has the required method set, undecidable by the fast tree-sitter pass) is a further deep
enrichment scip-go also computes; v1 overlays CALLS only, so the fast pass still emits no IMPLEMENTS
rather than guess. Suffix/exclude constants live here (single source of truth); git_sync imports
them back."""

from __future__ import annotations

from auryn.core.registry import GitLanguage, register_git_language

LANGUAGE = "go"
FILE_SUFFIXES = (".go",)
# Directories the `go` toolchain itself ignores (`vendor`, `testdata`) plus VCS/editor/build
# output. A `testdata` dir is not a Go package -- the go tool never compiles it.
EXCLUDED_DIR_NAMES = frozenset(
    {".git", "vendor", "node_modules", "testdata", "bin", "dist", ".idea", ".vscode"}
)


def _sync(*args, **kwargs):
    """Fast phase, deferred until a source has Go files -- imports the tree-sitter-go engine."""
    from auryn.ingestors.go.git_sync import treesitter_language_sync

    return treesitter_language_sync(*args, **kwargs)


def _deep_sync(*args, **kwargs):
    """Deep phase (scip-go / SCIP), deferred likewise -- degrades to the fast pass if unavailable."""
    from auryn.ingestors.go.git_sync import scip_go_language_sync

    return scip_go_language_sync(*args, **kwargs)


register_git_language(
    GitLanguage(
        language=LANGUAGE,
        file_suffixes=FILE_SUFFIXES,
        excluded_dir_names=EXCLUDED_DIR_NAMES,
        sync=_sync,
        deep_sync=_deep_sync,
    )
)
