"""Import-light Compose language registration -- the `auryn.languages` entry-point target.

Importing this registers the Compose GitLanguage without importing PyYAML: the lazy `sync` thunk
imports the engine only when a source actually contains a compose file.

Detection by suffix: `compose.yml`/`compose.yaml` matches the four canonical names -- `compose.yml`,
`compose.yaml`, `docker-compose.yml`, `docker-compose.yaml` (the `docker-` prefix is covered because
those paths still END with `compose.yml`/`compose.yaml`). The parser guards on a top-level `services:`
map, so a stray non-compose `*.compose.yml` that slips through produces nothing. A non-standard name
(`docker-compose.prod.yml`) is NOT matched -- the same basename-variant gap as Docker, resolved by the
`GitLanguage.filename_matches` follow-up (ADR 0007 §7)."""

from __future__ import annotations

from auryn.core.registry import GitLanguage, register_git_language

LANGUAGE = "compose"
FILE_SUFFIXES = ("compose.yml", "compose.yaml")
EXCLUDED_DIR_NAMES = frozenset({".git", "node_modules", "vendor", "dist", ".idea", ".vscode"})


def _sync(*args, **kwargs):
    from auryn.ingestors.compose.git_sync import _git_language_sync

    return _git_language_sync(*args, **kwargs)


register_git_language(
    GitLanguage(
        language=LANGUAGE,
        file_suffixes=FILE_SUFFIXES,
        excluded_dir_names=EXCLUDED_DIR_NAMES,
        sync=_sync,
    )
)
