"""Read-only access to a plain local directory's content, behind the exact same
small surface the git-based ingestion pipeline already consumes from GitRepoCache --
`list_files`, `read_blob`, and `working_tree` -- so a directory can be ingested with
no git involved at all.

The whole per-language pipeline (core/ingest.py -> a GitLanguage.sync -> the
Kendo stage / routing enrichment) only ever calls those three methods on its
"source"; `fetch`/`resolve_ref` live solely in the top-level git `sync()`, which the
ingestion endpoint bypasses. So a directory that implements this surface drops
straight in wherever a GitRepoCache goes (see core/ingest._ingest_from_source,
which is duck-typed over both).

Two deliberate choices keep the delta/caching machinery correct without a commit:
- Files are keyed by a sha256 of their *bytes*, not their path -- mirroring
  GitRepoCache's blob-sha semantics -- so the language parse caches (keyed by "blob
  sha") stay content-correct across renames/edits and a rename is still a cache hit.
- `tree_sha()` (a hash over the whole sorted (path, content_hash) list) stands in for
  a commit sha as the `target_sha`, so the per-project commit-sha short-circuit works
  exactly as for git: re-ingesting an unchanged directory is a no-op, and a changed
  file yields a precise delta.
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from auryn.core import generated
from auryn.core.source_filter import SourceFilter

logger = logging.getLogger(__name__)

# Never descend into a repo's git internals: for a directory that happens to be a
# working tree, `.git` holds thousands of objects that aren't source and would only
# add cost and noise to the content hash. GitRepoCache never sees these (it reads a
# bare clone's tree), so skipping them keeps the two sources' file sets aligned.
_ALWAYS_SKIPPED_DIRS = {".git"}


def log_skipped_generated(skipped: list[tuple[str, str]]) -> None:
    """One INFO line naming what was dropped as generated/vendored -- never a silent cut."""
    sample = ", ".join(f"{p} [{r}]" for p, r in skipped[:5])
    more = f" (+{len(skipped) - 5} more)" if len(skipped) > 5 else ""
    logger.info("generated/vendored: skipping %d file(s): %s%s", len(skipped), sample, more)


class LocalDirectorySource:
    """A directory-backed stand-in for GitRepoCache. The `sha` arguments to
    `list_files`/`working_tree` are accepted for signature-compatibility but ignored
    -- a plain directory has no commits, so content is always read from disk as it is
    right now. The directory is walked (and hashed) once, lazily, and the result
    cached for the lifetime of the instance; construct a new one to re-read."""

    def __init__(self, root: str | Path, file_filter: SourceFilter | None = None) -> None:
        self._root = Path(root).resolve()
        self._filter = file_filter or SourceFilter()
        self._entries: list[tuple[str, str]] = []  # (relative_posix_path, content_hash), sorted
        self._by_hash: dict[str, Path] = {}
        self._skipped_generated: list[tuple[str, str]] = []  # (path, reason) dropped as generated
        self._walked = False

    def _walk(self) -> None:
        if self._walked:
            return
        detect_generated = generated.detection_enabled()
        entries: list[tuple[str, str]] = []
        by_hash: dict[str, Path] = {}
        skipped: list[tuple[str, str]] = []
        for path in self._iter_candidate_files():
            data = path.read_bytes()
            rel = path.relative_to(self._root).as_posix()
            # Drop generated/vendored files before hashing, so they're absent from the file set,
            # the content hash (tree_sha), AND every language's listing (core.generated). The
            # bytes are already in hand here, so detection costs no extra read.
            if detect_generated:
                reason = generated.is_generated(rel, data)
                if reason:
                    skipped.append((rel, reason))
                    continue
            content_hash = hashlib.sha256(data).hexdigest()
            entries.append((rel, content_hash))
            by_hash[content_hash] = path
        entries.sort()
        self._entries = entries
        self._by_hash = by_hash
        self._skipped_generated = sorted(skipped)
        self._walked = True
        if skipped:
            log_skipped_generated(self._skipped_generated)

    def _iter_candidate_files(self) -> Iterator[Path]:
        """Absolute paths of the files to consider, *before* the generated/vendored filter. The base
        walks the whole tree, pruning only the always-skipped (`.git`) and user-excluded dirs -- a
        plain directory has no ignore semantics, so everything under it is fair game.
        `WorkingTreeSource` overrides this to git-visible files only (honoring `.gitignore`)."""
        pruned = _ALWAYS_SKIPPED_DIRS | self._filter.exclude_dirs
        for dirpath, dirnames, filenames in os.walk(self._root):
            # Prune skipped dirs in place so os.walk never descends into them.
            dirnames[:] = [d for d in dirnames if d not in pruned]
            for name in filenames:
                path = Path(dirpath) / name
                if path.is_file():  # skip broken symlinks, sockets, fifos
                    yield path

    def list_files(self, sha: str, suffix: str, excluded_dir_names: set[str]) -> list[tuple[str, str]]:
        """Every file under the directory whose path ends with `suffix`, excluding
        anything under a directory named in `excluded_dir_names` -- returns
        (relative_path, content_hash) pairs, matching GitRepoCache.list_files exactly
        (there content_hash is the git blob sha; here it's a sha256 of the bytes)."""
        self._walk()
        out: list[tuple[str, str]] = []
        for rel, content_hash in self._entries:
            if not rel.endswith(suffix):
                continue
            dir_parts = rel.split("/")[:-1]
            if excluded_dir_names & set(dir_parts):
                continue
            if not self._filter.allows(rel):  # user include/exclude (dirs + suffixes)
                continue
            out.append((rel, content_hash))
        return out

    def read_blob(self, content_hash: str) -> bytes:
        """The bytes of the file with this content hash. Since the key IS the content
        hash, identical files share a key and any of them serves the same bytes."""
        self._walk()
        path = self._by_hash.get(content_hash)
        if path is None:
            raise KeyError(f"no file with content hash {content_hash!r} under {self._root}")
        return path.read_bytes()

    @contextmanager
    def working_tree(self, sha: str) -> Iterator[Path]:
        """The directory itself is the working tree -- yielded directly, no copy (the
        git side materializes a temp checkout here instead). For tools that need real
        files on disk, e.g. the Roslyn extractor."""
        yield self._root

    def tree_sha(self) -> str:
        """A stable content hash over the whole directory -- the pseudo-commit used as
        `target_sha`. Depends only on the set of (relative path, file bytes), so it's
        identical iff the ingestable content is identical, making re-ingest a no-op
        and any file change a precise delta."""
        self._walk()
        h = hashlib.sha256()
        for rel, content_hash in self._entries:  # already sorted -> deterministic
            h.update(rel.encode("utf-8"))
            h.update(b"\0")
            h.update(content_hash.encode("ascii"))
            h.update(b"\0")
        return h.hexdigest()


class WorkingTreeSource(LocalDirectorySource):
    """A LocalDirectorySource that is *also* a git working tree: identical live-file reads and
    content-hash `tree_sha` (so the short-circuit and full path are unchanged), plus
    git-driven change detection for an incremental resync of the file-save / project-save
    trigger (ADR 0002).

    `changed_since(base_commit)` reports the working tree's divergence from a commit in one
    `git diff` -- both the committed moves since `base_commit` AND the current uncommitted
    edits -- which is exactly the set a save-triggered resync must reconcile. Untracked (new)
    files are added via `ls-files --others`, so a just-created, never-committed file counts."""

    @staticmethod
    def is_git_worktree(root: str | Path) -> bool:
        """True if `root` is inside a git working tree (a `.git` dir, or the `.git` file a
        linked worktree uses)."""
        return (Path(root) / ".git").exists()

    def _repo(self):
        import git

        return git.Repo(str(self._root))

    def _iter_candidate_files(self) -> Iterator[Path]:
        """Only git-visible files: tracked + untracked-but-not-ignored (`git ls-files --cached
        --others --exclude-standard`), so `.gitignore`d trees -- `.venv`, `node_modules`, `build/`,
        `dist/` -- are never walked, read, or hashed. (A plain `os.walk` would read+sha256 the whole
        `.venv` on every ingest, even though the per-language dir filter drops it afterwards.) This
        also keeps `tree_sha` -- and thus the re-ingest no-op check -- keyed on exactly the working
        set we ingest. Falls back to the base full walk if git can't answer."""
        try:
            out = self._repo().git.ls_files("--cached", "--others", "--exclude-standard", "-z")
        except Exception as e:  # git missing / corrupt repo / unusual worktree -> don't lose the ingest
            logger.warning("git ls-files failed (%s); walking the full directory instead", e)
            yield from super()._iter_candidate_files()
            return
        pruned = _ALWAYS_SKIPPED_DIRS | self._filter.exclude_dirs
        for rel in out.split("\0"):
            if not rel or pruned.intersection(rel.split("/")[:-1]):
                continue
            path = self._root / rel
            if path.is_file():  # a submodule gitlink or a tracked-but-deleted path is not a file
                yield path

    def head(self) -> str:
        """The current HEAD commit sha -- the base a save-triggered resync diffs against."""
        return self._repo().head.commit.hexsha

    def changed_since(self, base_commit: str, suffix: str) -> dict:
        """{modified, added, deleted, renamed} of `suffix` files that differ between
        `base_commit` and the working tree right now. Raises git.GitCommandError if
        `base_commit` is unreachable -- the caller treats that as "fall back to full resync"."""
        from auryn.core.git_source import parse_name_status

        repo = self._repo()
        changes = parse_name_status(repo.git.diff("-M", "--name-status", base_commit), suffix)
        for rel in repo.git.ls_files("--others", "--exclude-standard").splitlines():
            if rel.endswith(suffix):
                changes["added"].append(rel)
        return changes
