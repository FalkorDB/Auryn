"""Import-light PL/SQL language registration -- the `auryn.languages` entry-point target.

Importing this module registers the plsql GitLanguage without importing the vendored ANTLR
grammar or oracledb: the GitLanguage's `sync` is a lazy thunk that imports
auryn.ingestors.plsql.git_sync only when a source actually contains PL/SQL files. The
live-DB ingestors (PLSQLIngestor as `oracle`, PLSQLFileIngestor as `plsql`) register
separately via the `auryn.ingestors` group, only on the `auryn run` path. Suffixes
come from the language's light config module.
"""

from __future__ import annotations

from auryn.core.registry import GitLanguage, register_git_language
from auryn.ingestors.plsql.config import PLSQL_FILE_SUFFIXES

LANGUAGE = "plsql"
EXCLUDED_DIR_NAMES: frozenset[str] = frozenset()


def _sync(*args, **kwargs):
    """Deferred until a source has PL/SQL files -- imports the ANTLR grammar engine."""
    from auryn.ingestors.plsql.git_sync import _git_language_sync

    return _git_language_sync(*args, **kwargs)


register_git_language(
    GitLanguage(
        language=LANGUAGE,
        file_suffixes=PLSQL_FILE_SUFFIXES,
        excluded_dir_names=EXCLUDED_DIR_NAMES,
        sync=_sync,
    )
)
