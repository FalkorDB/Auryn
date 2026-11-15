"""Import-light Go language registration -- the `auryn.languages` entry-point target.

Importing this module registers the Go GitLanguage without importing tree-sitter: the
GitLanguage's `sync` is a lazy thunk that imports auryn.ingestors.go.git_sync only when a
source actually contains `.go` files. Single-phase today (fast tree-sitter-go).

A deep pass -- a `go/types` or gopls-backed engine -- would slot in here as `deep_sync`
(making Go two-phase like Python). It is where **IMPLEMENTS** belongs: Go interface satisfaction
is *structural/implicit* (a type implements an interface iff it has the required method set),
which the fast tree-sitter pass cannot decide without real type information -- so the fast pass
deliberately emits no IMPLEMENTS edge rather than guess (exactly as Java defers its Eclipse
JDT/javac pass). The deep pass would also resolve cross-file/interface-dispatch calls and
inferred local-variable types.

Suffix/exclude constants live here (single source of truth); git_sync imports them back."""

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
    """Deferred until a source has Go files -- imports the tree-sitter-go engine."""
    from auryn.ingestors.go.git_sync import _git_language_sync

    return _git_language_sync(*args, **kwargs)


register_git_language(
    GitLanguage(
        language=LANGUAGE,
        file_suffixes=FILE_SUFFIXES,
        excluded_dir_names=EXCLUDED_DIR_NAMES,
        sync=_sync,
    )
)
