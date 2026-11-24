"""Import-light C++ language registration -- the `auryn.languages` entry-point target.

Importing this module registers the C++ GitLanguage without importing tree-sitter: the
GitLanguage's `sync`/`deep_sync` are lazy thunks that import auryn.ingestors.cpp.git_sync only
when a source actually contains C++ files. Two-phase (like Python/C#/Rust/TS/Go/Java): the fast
`sync` is tree-sitter-cpp (CALLS by name); the deep `deep_sync` resolves CALLS precisely via
scip-clang's SCIP index (overload resolution, virtual dispatch, cross-TU/header<->source calls),
degrading to the fast pass when scip-clang or a compile_commands.json is absent. It is an explicit
opt-in (AURYN_SCIP_CLANG) because scip-clang runs Clang with the repo's compile_commands.json
flags. Suffix/exclude constants live here (single source of truth); git_sync imports them back.

SUFFIX SPLIT WITH THE C INGESTOR: the C plugin already claims `.c` and `.h`, so this plugin
claims ONLY unambiguous C++ suffixes and leaves `.c`/`.h` to C. Known limitation: a C++ project
that puts class declarations in `.h` headers (rather than `.hpp`) won't get those headers'
classes extracted here -- they're routed to the C ingestor, which has no class model. Projects
using `.hpp`/`.hh`/... for headers are fully covered."""

from __future__ import annotations

from auryn.core.registry import GitLanguage, register_git_language

LANGUAGE = "cpp"
# Only unambiguous C++ suffixes -- `.c`/`.h` stay with the C ingestor (see the module docstring).
FILE_SUFFIXES = (".cpp", ".cc", ".cxx", ".c++", ".hpp", ".hh", ".hxx", ".h++", ".ipp", ".tpp", ".inl")
EXCLUDED_DIR_NAMES = frozenset(
    {".git", "build", "cmake-build-debug", "node_modules", "vendor", "third_party", "external", "out"}
)


def _sync(*args, **kwargs):
    """Fast phase, deferred until a source has C++ files -- imports the tree-sitter-cpp engine."""
    from auryn.ingestors.cpp.git_sync import treesitter_language_sync

    return treesitter_language_sync(*args, **kwargs)


def _deep_sync(*args, **kwargs):
    """Deep phase (scip-clang / SCIP), deferred likewise -- degrades to the fast pass if unavailable."""
    from auryn.ingestors.cpp.git_sync import scip_clang_language_sync

    return scip_clang_language_sync(*args, **kwargs)


register_git_language(
    GitLanguage(
        language=LANGUAGE,
        file_suffixes=FILE_SUFFIXES,
        excluded_dir_names=EXCLUDED_DIR_NAMES,
        sync=_sync,
        deep_sync=_deep_sync,
    )
)
