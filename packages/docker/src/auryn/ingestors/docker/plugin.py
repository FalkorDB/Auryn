"""Import-light Docker language registration -- the `auryn.languages` entry-point target.

Importing this module registers the Docker GitLanguage without importing the parser: the
GitLanguage's `sync` is a lazy thunk that imports auryn.ingestors.docker.git_sync only when a source
actually contains Dockerfiles. Single-phase (declarative -- no deep pass).

Detection uses suffix matching (`path.endswith`), which catches `Dockerfile`, `web.Dockerfile`, and
`*.dockerfile`. Two KNOWN gaps, both resolved by the optional `GitLanguage.filename_matches` predicate
proposed in ADR 0007 §7 (every IaC producer needs basename matching), deferred until that lands:
  - the `Dockerfile.prod` / `Dockerfile.dev` basename-variant family isn't matched by a pure suffix;
  - an extensionless `Dockerfile` has an empty `Path.suffix`, so the save-watcher's suffix-based
    `note_change` won't trigger an incremental resync on edit (a full `index`/commit resync still
    picks it up). Both want basename-aware detection, not a suffix."""

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
