"""Import-light CUDA language registration -- the `auryn.languages` entry-point target.

Importing this module registers the CUDA GitLanguage without importing tree-sitter: the
GitLanguage's `sync` is a lazy thunk that imports auryn.ingestors.cuda.git_sync only when a
source actually contains CUDA files. Single-phase today (fast tree-sitter-cuda, reusing the
tree-sitter-cpp extraction engine since CUDA is a strict superset of C++); a deep libclang/nvcc
pass -- template instantiation, overload resolution, precise device/host linking -- would slot in
here as `deep_sync`. Suffix/exclude constants live here (single source of truth); git_sync imports
them back.

SUFFIX SPLIT: CUDA owns the unambiguous `.cu`/`.cuh` suffixes only. A `.cpp`/`.cc`/`.h` file that
happens to contain CUDA (e.g. host code using the runtime API) stays with the C/C++ ingestors --
those parse fine without the CUDA grammar; only `.cu`/`.cuh` need it (for `<<<...>>>` kernel
launches and device qualifiers)."""

from __future__ import annotations

from auryn.core.registry import GitLanguage, register_git_language

LANGUAGE = "cuda"
# Unambiguous CUDA suffixes only -- `.cu` source, `.cuh` header. No overlap with C/C++.
FILE_SUFFIXES = (".cu", ".cuh")
EXCLUDED_DIR_NAMES = frozenset(
    {".git", "build", "cmake-build-debug", "node_modules", "vendor", "third_party", "external", "out"}
)


def _sync(*args, **kwargs):
    """Deferred until a source has CUDA files -- imports the tree-sitter-cuda engine."""
    from auryn.ingestors.cuda.git_sync import _git_language_sync

    return _git_language_sync(*args, **kwargs)


register_git_language(
    GitLanguage(
        language=LANGUAGE,
        file_suffixes=FILE_SUFFIXES,
        excluded_dir_names=EXCLUDED_DIR_NAMES,
        sync=_sync,
    )
)
