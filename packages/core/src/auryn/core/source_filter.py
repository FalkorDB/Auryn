"""User-specified allow/deny filters for what a source exposes -- by directory name and by
file suffix. Applied at the *source* layer (GitRepoCache / LocalDirectorySource.list_files) so
every language ingestor, the language-detection scan, and the unsupported-file scan all honor
the same policy, without threading the lists through each language's sync.

Directory names match at ANY path level, exactly like the ingestors' own `excluded_dir_names`
(so `--exclude-dir deps` drops every `deps/` anywhere in the tree). Suffixes are normalized to
a leading dot. An empty allow-list means "allow all"; deny-lists always subtract. When both an
allow and a deny name the same thing, deny wins.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


def _norm_suffix(s: str) -> str:
    s = s.strip()
    if not s:
        return ""
    return s if s.startswith(".") else "." + s


@dataclass(frozen=True)
class SourceFilter:
    include_dirs: frozenset[str] = frozenset()       # whitelist: keep only files under one of these dirs
    exclude_dirs: frozenset[str] = frozenset()       # blacklist: drop files under any of these dirs
    include_suffixes: frozenset[str] = frozenset()   # whitelist file types (".c", ".py", ...)
    exclude_suffixes: frozenset[str] = frozenset()   # blacklist file types

    @classmethod
    def build(
        cls,
        *,
        include_dirs: Iterable[str] = (),
        exclude_dirs: Iterable[str] = (),
        include_suffixes: Iterable[str] = (),
        exclude_suffixes: Iterable[str] = (),
    ) -> "SourceFilter":
        norm = lambda ss: frozenset(x for x in (_norm_suffix(s) for s in ss) if x)  # noqa: E731
        return cls(
            include_dirs=frozenset(d for d in include_dirs if d),
            exclude_dirs=frozenset(d for d in exclude_dirs if d),
            include_suffixes=norm(include_suffixes),
            exclude_suffixes=norm(exclude_suffixes),
        )

    def is_empty(self) -> bool:
        return not (self.include_dirs or self.exclude_dirs or self.include_suffixes or self.exclude_suffixes)

    def allows(self, rel_path: str) -> bool:
        """Does this repo-relative path pass the filter? Deny beats allow."""
        if self.include_suffixes and not any(rel_path.endswith(s) for s in self.include_suffixes):
            return False
        if any(rel_path.endswith(s) for s in self.exclude_suffixes):
            return False
        segments = set(rel_path.replace("\\", "/").split("/")[:-1])  # directory names on the path
        if self.exclude_dirs & segments:
            return False
        if self.include_dirs and not (self.include_dirs & segments):
            return False
        return True

    def describe(self) -> str:
        parts = []
        if self.include_dirs:
            parts.append(f"only-dirs={sorted(self.include_dirs)}")
        if self.exclude_dirs:
            parts.append(f"skip-dirs={sorted(self.exclude_dirs)}")
        if self.include_suffixes:
            parts.append(f"only-ext={sorted(self.include_suffixes)}")
        if self.exclude_suffixes:
            parts.append(f"skip-ext={sorted(self.exclude_suffixes)}")
        return ", ".join(parts) or "no filter"
