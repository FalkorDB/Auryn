"""Import-light Kubernetes language registration -- the `auryn.languages` entry-point target.

Importing this registers the k8s GitLanguage without importing PyYAML: the lazy `sync` thunk imports
the engine only when a source actually contains `.yaml`/`.yml` files.

Detection is broad by suffix (`*.yaml`/`*.yml`) because k8s manifests have no distinctive filename --
the parser's `apiVersion`+`kind` guard is what actually identifies a manifest, and git_sync drops any
YAML that yields no workloads/ingresses. (A tighter, directory-/content-aware detector is the
`GitLanguage.filename_matches` follow-up in ADR 0007 §7.) Non-manifest YAML (compose, CI configs) is
scanned but produces nothing."""

from __future__ import annotations

from auryn.core.registry import GitLanguage, register_git_language

LANGUAGE = "k8s"
FILE_SUFFIXES = (".yaml", ".yml")
EXCLUDED_DIR_NAMES = frozenset({".git", "node_modules", "vendor", "dist", ".idea", ".vscode"})


def _sync(*args, **kwargs):
    from auryn.ingestors.k8s.git_sync import _git_language_sync

    return _git_language_sync(*args, **kwargs)


register_git_language(
    GitLanguage(
        language=LANGUAGE,
        file_suffixes=FILE_SUFFIXES,
        excluded_dir_names=EXCLUDED_DIR_NAMES,
        sync=_sync,
    )
)
