"""Import-light Compose language registration -- the `auryn.languages` entry-point target.

Importing this registers the Compose GitLanguage without importing PyYAML: the lazy `sync` thunk
imports the engine only when a source actually contains a compose file.

Detection (basename-aware, ADR 0007 §7): the `compose.yml`/`compose.yaml` suffix matches the four
canonical names (`compose.yml`/`.yaml`, `docker-compose.yml`/`.yaml` -- the `docker-` prefix still ENDS
with `compose.yml`); `file_globs` adds the overlay-variant family (`compose.prod.yml`,
`docker-compose.override.yaml`). The parser guards on a top-level `services:` map, so a stray
non-compose name that slips through the glob produces nothing."""

from __future__ import annotations

from auryn.core.registry import GitLanguage, match_patterns, register_git_language

LANGUAGE = "compose"
FILE_SUFFIXES = ("compose.yml", "compose.yaml")
FILE_GLOBS = ("*compose.*.yml", "*compose.*.yaml")  # overlays: compose.prod.yml, docker-compose.override.yaml
MATCH_PATTERNS = match_patterns(FILE_SUFFIXES, FILE_GLOBS)
EXCLUDED_DIR_NAMES = frozenset({".git", "node_modules", "vendor", "dist", ".idea", ".vscode"})


def _sync(*args, **kwargs):
    from auryn.ingestors.compose.git_sync import _git_language_sync

    return _git_language_sync(*args, **kwargs)


register_git_language(
    GitLanguage(
        language=LANGUAGE,
        file_suffixes=FILE_SUFFIXES,
        file_globs=FILE_GLOBS,
        excluded_dir_names=EXCLUDED_DIR_NAMES,
        sync=_sync,
    )
)
