"""Import-light CI language registration -- the `auryn.languages` entry-point target.

Importing this registers the CI GitLanguage without importing PyYAML: the lazy `sync` thunk imports the
engine only when a source actually contains workflows.

Detection ANCHORS on the `.github/workflows/` path (a `file_globs` PATH glob, ADR 0007 §7 / the
filename_matches path extension): CI fires only on a repo that has GitHub Actions workflows, not on
every `.yml`. A workflow carries a top-level `jobs:` map (not `apiVersion`+`kind` nor `services:`), so
the k8s/compose ingestors drop it -- the producers coexist on distinct id prefixes. v1 is GitHub
Actions; other CI systems (GitLab CI, Jenkins) are future dialects."""

from __future__ import annotations

from auryn.core.registry import GitLanguage, register_git_language

LANGUAGE = "ci"
FILE_SUFFIXES = ()
FILE_GLOBS = (".github/workflows/*.yml", ".github/workflows/*.yaml")  # path-anchored on the workflows dir
EXCLUDED_DIR_NAMES = frozenset({".git", "node_modules", "vendor", "dist", ".idea", ".vscode"})


def _sync(*args, **kwargs):
    from auryn.ingestors.ci.git_sync import _git_language_sync

    return _git_language_sync(*args, **kwargs)


register_git_language(
    GitLanguage(language=LANGUAGE, file_suffixes=FILE_SUFFIXES, file_globs=FILE_GLOBS,
                excluded_dir_names=EXCLUDED_DIR_NAMES, sync=_sync)
)
