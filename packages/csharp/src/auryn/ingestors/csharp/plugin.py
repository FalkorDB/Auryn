"""Import-light C# language registration -- the `auryn.languages` entry-point target.

Importing this module registers the C# GitLanguage without importing tree-sitter or the
Roslyn extractor: the GitLanguage's `sync` is a lazy thunk that imports
auryn.ingestors.csharp.git_sync only when a source actually contains C# files. The
live-DB CSharpIngestor (ingestor.py) registers separately, via the `auryn.ingestors`
group, only on the `auryn run` path. Suffix/exclude constants live here (the single
source of truth); git_sync imports them back.
"""

from __future__ import annotations

from auryn.core.registry import GitLanguage, register_git_language

LANGUAGE = "csharp"
# `.cshtml` (the Kendo/MVC view tier) is owned by C# too -- it's ingested as a stage of the
# C# git-sync. Declaring it here makes the endpoint detect a Kendo-only repo as C# and stops
# it reporting `.cshtml` as "unsupported". The C# parse paths filter `.cs` themselves, so
# this only affects language detection.
FILE_SUFFIXES = (".cs", ".cshtml")
EXCLUDED_DIR_NAMES = frozenset({"bin", "obj"})


def _sync(*args, **kwargs):
    """Fast phase (tree-sitter, no .NET SDK): structural backbone + statically-resolvable
    relationships. Runs on `--deep off`. Imported lazily so discovery stays engine-free."""
    from auryn.ingestors.csharp.git_sync import treesitter_language_sync

    return treesitter_language_sync(*args, **kwargs)


def _deep_sync(*args, **kwargs):
    """Deep phase (Roslyn): semantic analysis + DI resolution layered over the fast pass in
    place. Falls back to tree-sitter automatically when the .NET SDK / extractor is unavailable.
    This gives C# the same fast/deep contract as Python (ast->jedi): `--deep off` = fast only."""
    from auryn.ingestors.csharp.git_sync import roslyn_language_sync

    return roslyn_language_sync(*args, **kwargs)


register_git_language(
    GitLanguage(
        language=LANGUAGE,
        file_suffixes=FILE_SUFFIXES,
        excluded_dir_names=EXCLUDED_DIR_NAMES,
        sync=_sync,
        deep_sync=_deep_sync,
    )
)
