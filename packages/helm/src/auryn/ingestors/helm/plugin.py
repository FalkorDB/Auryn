"""Import-light Helm language registration -- the `auryn.languages` entry-point target.

Importing this registers the helm GitLanguage without importing PyYAML: the lazy `sync` thunk imports
the engine only when a source actually contains `.yaml`/`.yml` files.

Detection is broad by suffix (`*.yaml`/`*.yml`, like k8s) because a chart has no single distinctive
file extension -- the `Chart.yaml` BASENAME is what git_sync uses to anchor a chart, and non-chart YAML
produces nothing. (A tighter, filename-aware detector is the `GitLanguage.filename_matches` follow-up
in ADR 0007 §7.) A chart's templates are Go-templated k8s manifests, so the k8s ingestor's YAML parse
drops them -- the two producers coexist, keyed on distinct `helm://` / `k8s://` id prefixes."""

from __future__ import annotations

from auryn.core.registry import GitLanguage, register_git_language

LANGUAGE = "helm"
FILE_SUFFIXES = (".yaml", ".yml")
EXCLUDED_DIR_NAMES = frozenset({".git", "node_modules", "vendor", "dist", ".idea", ".vscode"})


def _sync(*args, **kwargs):
    from auryn.ingestors.helm.git_sync import _git_language_sync

    return _git_language_sync(*args, **kwargs)


register_git_language(
    GitLanguage(language=LANGUAGE, file_suffixes=FILE_SUFFIXES, excluded_dir_names=EXCLUDED_DIR_NAMES, sync=_sync)
)
