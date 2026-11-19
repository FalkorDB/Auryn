"""Import-light Terraform language registration -- the `auryn.languages` entry-point target. The lazy
`sync` thunk imports python-hcl2 only when a source contains `.tf` files."""

from __future__ import annotations

from auryn.core.registry import GitLanguage, register_git_language

LANGUAGE = "terraform"
FILE_SUFFIXES = (".tf",)
EXCLUDED_DIR_NAMES = frozenset({".git", "node_modules", "vendor", "dist", ".idea", ".vscode", ".terraform"})


def _sync(*args, **kwargs):
    from auryn.ingestors.terraform.git_sync import _git_language_sync

    return _git_language_sync(*args, **kwargs)


register_git_language(
    GitLanguage(language=LANGUAGE, file_suffixes=FILE_SUFFIXES, excluded_dir_names=EXCLUDED_DIR_NAMES, sync=_sync)
)
