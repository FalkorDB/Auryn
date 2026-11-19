"""Import-light Helm language registration -- the `auryn.languages` entry-point target.

Importing this registers the helm GitLanguage without importing PyYAML: the lazy `sync` thunk imports
the engine only when a source actually contains `.yaml`/`.yml` files.

Detection matches the chart FILES by basename (`file_globs`, ADR 0007 §7) -- `Chart.yaml`/`Chart.yml`
and the sibling `values.yaml`/`values.yml` -- instead of the old broad `*.yaml` (which, like k8s,
woke helm on every YAML in the repo and dropped the non-charts). helm now fires only on chart-related
files; a plain k8s manifest / compose file / arbitrary `config.yaml` no longer wakes it. Including
`values.yaml` also makes the save-watcher resync on a values edit (where a chart's images/config live)
and lets a resync that still sees `values.yaml` after `Chart.yaml` is deleted clean the chart up; the
whole-chart-removed case (no files at all) is handled by the `helm://` prior-graph cleanup in
core.ingest. A chart's Go-templated templates are invalid YAML, so the k8s parse drops them -- the two
producers coexist on distinct `helm://` / `k8s://` id prefixes."""

from __future__ import annotations

from auryn.core.registry import GitLanguage, register_git_language

LANGUAGE = "helm"
FILE_SUFFIXES = ()
# Chart-related basenames only (NOT a broad `*.yaml`): the chart manifest + its values. A chart always
# has a Chart.yaml, so detection still fires only on real charts; git_sync anchors each chart on its
# Chart.yaml and reads the sibling values.yaml.
FILE_GLOBS = ("Chart.yaml", "Chart.yml", "values.yaml", "values.yml")
CHART_GLOBS = FILE_GLOBS
EXCLUDED_DIR_NAMES = frozenset({".git", "node_modules", "vendor", "dist", ".idea", ".vscode"})


def _sync(*args, **kwargs):
    from auryn.ingestors.helm.git_sync import _git_language_sync

    return _git_language_sync(*args, **kwargs)


register_git_language(
    GitLanguage(language=LANGUAGE, file_suffixes=FILE_SUFFIXES, file_globs=FILE_GLOBS,
                excluded_dir_names=EXCLUDED_DIR_NAMES, sync=_sync)
)
