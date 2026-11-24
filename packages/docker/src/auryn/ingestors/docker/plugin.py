"""Import-light Docker language registration -- the `auryn.languages` entry-point target.

Importing this module registers the Docker GitLanguage without importing the parser: the
GitLanguage's `sync` is a lazy thunk that imports auryn.ingestors.docker.git_sync only when a source
actually contains Dockerfiles. Single-phase (declarative -- no deep pass).

Detection is basename-aware (`GitLanguage.file_globs`, ADR 0007 §7): beyond the `Dockerfile` /
`web.Dockerfile` / `*.dockerfile` the old suffix caught, `file_globs` adds the `Dockerfile.prod` /
`Dockerfile.dev` basename-variant family and `Containerfile` (Podman/Buildah, identical syntax). The
save-watcher also matches by basename now, so an extensionless `Dockerfile` edit (empty `Path.suffix`)
triggers an incremental resync."""

from __future__ import annotations

from auryn.core.registry import GitLanguage, match_patterns, register_git_language

LANGUAGE = "docker"
FILE_SUFFIXES = ("Dockerfile", ".dockerfile")
FILE_GLOBS = ("Dockerfile.*", "Containerfile", "Containerfile.*")
MATCH_PATTERNS = match_patterns(FILE_SUFFIXES, FILE_GLOBS)
EXCLUDED_DIR_NAMES = frozenset({".git", "node_modules", "vendor", "dist", ".idea", ".vscode"})


def _sync(*args, **kwargs):
    """Deferred until a source has Dockerfiles -- imports the parser engine."""
    from auryn.ingestors.docker.git_sync import _git_language_sync

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
