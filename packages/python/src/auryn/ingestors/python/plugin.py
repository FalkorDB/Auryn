"""Import-light Python language registration -- the `auryn.languages` entry-point target.

Importing this module registers the Python GitLanguage but pulls in NO extraction engine
(neither the stdlib-`ast` pass nor Jedi): the GitLanguage's `sync` is a lazy thunk that
imports auryn.ingestors.python.git_sync only when a source actually contains `.py`
files. That is what keeps a repo with no Python from paying Python's (and Jedi's) import
cost during discovery. Suffix/exclude constants live here (the single source of truth);
git_sync imports them back.
"""

from __future__ import annotations

from auryn.core.registry import GitLanguage, register_git_language

LANGUAGE = "python"
FILE_SUFFIXES = (".py",)
EXCLUDED_DIR_NAMES = frozenset(
    {
        "__pycache__", ".venv", "venv", ".tox", ".nox", ".mypy_cache", ".pytest_cache",
        ".git", "build", "dist", ".eggs", "node_modules", "site-packages",
    }
)


def _sync(*args, **kwargs):
    """Fast phase (stdlib `ast`): structural backbone + statically-resolvable
    inheritance/calls, zero extra deps. Imported lazily so discovery stays engine-free."""
    from auryn.ingestors.python.git_sync import ast_language_sync

    return ast_language_sync(*args, **kwargs)


def _deep_sync(*args, **kwargs):
    """Deep phase (Jedi): layers inferred variable/attribute/param types and resolved
    (incl. receiver-typed) calls over the fast pass, upgrading the same node ids in place."""
    from auryn.ingestors.python.git_sync import jedi_language_sync

    return jedi_language_sync(*args, **kwargs)


register_git_language(
    GitLanguage(
        language=LANGUAGE,
        file_suffixes=FILE_SUFFIXES,
        excluded_dir_names=EXCLUDED_DIR_NAMES,
        sync=_sync,
        deep_sync=_deep_sync,
    )
)
