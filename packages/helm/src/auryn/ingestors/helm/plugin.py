"""Import-light Helm language registration -- the `auryn.languages` entry-point target.

Importing this registers the helm GitLanguage without importing PyYAML: the lazy `sync` thunk imports
the engine only when a source actually contains `.yaml`/`.yml` files.

Detection ANCHORS on the `Chart.yaml` basename (`file_globs`, ADR 0007 §7): helm fires only when a
repo actually contains a chart, instead of being woken by every `.yaml` (the old broad suffix, like
k8s, scanned all YAML and dropped non-charts). git_sync then reads each chart's sibling `values.yaml`
(`CHART_GLOBS`). A chart's templates are Go-templated k8s manifests, so the k8s ingestor's YAML parse
drops them -- the two producers coexist, keyed on distinct `helm://` / `k8s://` id prefixes."""

from __future__ import annotations

from auryn.core.registry import GitLanguage, register_git_language

LANGUAGE = "helm"
FILE_SUFFIXES = ()
FILE_GLOBS = ("Chart.yaml", "Chart.yml")           # the anchor: fire only when a chart exists
CHART_GLOBS = FILE_GLOBS + ("values.yaml", "values.yml")  # git_sync also reads each chart's values.yaml
EXCLUDED_DIR_NAMES = frozenset({".git", "node_modules", "vendor", "dist", ".idea", ".vscode"})


def _sync(*args, **kwargs):
    from auryn.ingestors.helm.git_sync import _git_language_sync

    return _git_language_sync(*args, **kwargs)


register_git_language(
    GitLanguage(language=LANGUAGE, file_suffixes=FILE_SUFFIXES, file_globs=FILE_GLOBS,
                excluded_dir_names=EXCLUDED_DIR_NAMES, sync=_sync)
)
