"""Import-light C++ language registration -- the `auryn.languages` entry-point target.

Importing this module registers the C++ GitLanguage without importing tree-sitter: the
GitLanguage's `sync` is a lazy thunk that imports auryn.ingestors.cpp.git_sync only when a
source actually contains C++ files. Single-phase today (fast tree-sitter-cpp); a deep libclang
pass -- template instantiation, overload resolution, ADL, precise header<->source linking,
cross-TU calls (needing a compile_commands.json) -- would slot in here as `deep_sync` (making
C++ two-phase like Python). Suffix/exclude constants live here (single source of truth);
git_sync imports them back.

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
    """Deferred until a source has C++ files -- imports the tree-sitter-cpp engine."""
    from auryn.ingestors.cpp.git_sync import _git_language_sync

    return _git_language_sync(*args, **kwargs)


register_git_language(
    GitLanguage(
        language=LANGUAGE,
        file_suffixes=FILE_SUFFIXES,
        excluded_dir_names=EXCLUDED_DIR_NAMES,
        sync=_sync,
    )
)
