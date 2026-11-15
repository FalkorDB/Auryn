"""Import-light Bash/Shell language registration -- the `auryn.languages` entry-point target.

Importing this module registers the Bash GitLanguage without importing tree-sitter: the
GitLanguage's `sync` is a lazy thunk that imports auryn.ingestors.bash.git_sync only when a
source actually contains `.sh`/`.bash` files. Single-phase today (fast tree-sitter-bash).

Bash has NO types and NO classes, so there is no class/type model to build: the useful graph is
structural and flat -- the functions a script defines, the local functions and external commands
each one invokes (CALLS), and the files it pulls in with `source`/`.` (IMPORTS). A deep pass -- a
real shell resolver that tracks `source` scope, `PATH`, aliases and dynamic command names -- would
slot in here as `deep_sync` (making Bash two-phase like Python), but the fast pass deliberately
guesses nothing it cannot read straight off the syntax tree.

Suffix/exclude constants live here (single source of truth); git_sync imports them back."""

from __future__ import annotations

from auryn.core.registry import GitLanguage, register_git_language

LANGUAGE = "bash"
# Suffix-based detection (the common, unambiguous case). Shebang-only scripts with no extension
# are intentionally out of scope for the fast pass -- keeping detection cheap and deterministic.
FILE_SUFFIXES = (".sh", ".bash")
EXCLUDED_DIR_NAMES = frozenset({".git", "node_modules", "vendor", "dist", ".idea", ".vscode"})


def _sync(*args, **kwargs):
    """Deferred until a source has Bash files -- imports the tree-sitter-bash engine."""
    from auryn.ingestors.bash.git_sync import _git_language_sync

    return _git_language_sync(*args, **kwargs)


register_git_language(
    GitLanguage(
        language=LANGUAGE,
        file_suffixes=FILE_SUFFIXES,
        excluded_dir_names=EXCLUDED_DIR_NAMES,
        sync=_sync,
    )
)
