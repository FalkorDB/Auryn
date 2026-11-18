"""Import-light Docker language registration -- the `auryn.languages` entry-point target.

Importing this module registers the Docker GitLanguage without importing the parser: the
GitLanguage's `sync` is a lazy thunk that imports auryn.ingestors.docker.git_sync only when a source
actually contains Dockerfiles. Single-phase (declarative -- no deep pass).

Detection uses suffix matching (`path.endswith`), which catches `Dockerfile`, `web.Dockerfile`, and
`*.dockerfile`. The `Dockerfile.prod` / `Dockerfile.dev` basename-variant family is NOT matched by a
pure suffix -- the clean fix is the optional `GitLanguage.filename_matches` predicate proposed in ADR
0007 §7 (every IaC producer needs basename matching). Until that lands, those variants are a known
gap."""

from __future__ import annotations

from auryn.core.registry import GitLanguage, register_git_language

LANGUAGE = "docker"
FILE_SUFFIXES = ("Dockerfile", ".dockerfile")
EXCLUDED_DIR_NAMES = frozenset({".git", "node_modules", "vendor", "dist", ".idea", ".vscode"})


def _sync(*args, **kwargs):
    """Deferred until a source has Dockerfiles -- imports the parser engine."""
    from auryn.ingestors.docker.git_sync import _git_language_sync

    return _git_language_sync(*args, **kwargs)


register_git_language(
    GitLanguage(
        language=LANGUAGE,
        file_suffixes=FILE_SUFFIXES,
        excluded_dir_names=EXCLUDED_DIR_NAMES,
        sync=_sync,
    )
)
