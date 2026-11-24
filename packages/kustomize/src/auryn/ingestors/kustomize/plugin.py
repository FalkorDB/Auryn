"""Import-light Kustomize language registration -- the `auryn.languages` entry-point target.

Importing this registers the kustomize GitLanguage without importing PyYAML: the lazy `sync` thunk
imports the engine only when a source actually contains a kustomization.

Detection ANCHORS on the `kustomization.yaml`/`kustomization.yml`/`Kustomization` basename (`file_globs`,
ADR 0007 §7) -- the names Kustomize itself recognizes -- so it fires only on real kustomizations, not
every `.yaml`. A `kustomization.yaml` carries `kind: Kustomization` (not a workload kind), so the k8s
ingestor parses it, finds no workload/ingress and drops it -- the two coexist on distinct `kustomize://`
/ `k8s://` id prefixes."""

from __future__ import annotations

from auryn.core.registry import GitLanguage, register_git_language

LANGUAGE = "kustomize"
FILE_SUFFIXES = ()
FILE_GLOBS = ("kustomization.yaml", "kustomization.yml", "Kustomization")
EXCLUDED_DIR_NAMES = frozenset({".git", "node_modules", "vendor", "dist", ".idea", ".vscode"})


def _sync(*args, **kwargs):
    from auryn.ingestors.kustomize.git_sync import _git_language_sync

    return _git_language_sync(*args, **kwargs)


register_git_language(
    GitLanguage(language=LANGUAGE, file_suffixes=FILE_SUFFIXES, file_globs=FILE_GLOBS,
                excluded_dir_names=EXCLUDED_DIR_NAMES, sync=_sync)
)
