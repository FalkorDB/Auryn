"""Read-only access to a git repository's content, sourced entirely from a bare
local clone -- no working tree is ever materialized. Moving between any two
commits/branches is therefore the same operation (there's no working-tree state to
reconcile), which is what makes the sync mechanism in
ingestors/csharp/git_sync.py correct regardless of direction.

Every behavior here was confirmed empirically against a real local repo before
being relied on (this project's established practice of verifying third-party/
tool behavior rather than assuming it -- see e.g. the tree-sitter-c-sharp field-name
quirks documented in ingestors/csharp/file_parser.py). In particular:
- `git clone --bare` does NOT set up a `remote.origin.fetch` refspec the way a
  normal clone does, so a plain `fetch()` afterwards updates nothing under
  refs/heads/* (only FETCH_HEAD, a one-off ref). The refspec has to be set
  explicitly, and only *after* the initial clone completes -- setting it up front
  conflicts with bare clone's own ref-copying and the clone fails outright with
  "multiple updates for ref ... not allowed".
- The bare clone's own `HEAD` only reflects the remote's default branch as of
  clone time and never updates on fetch, even if the remote's actual default
  branch changes later. Resolving "the default branch" has to go straight to the
  remote via `git ls-remote --symref <uri> HEAD`, not the local clone's HEAD.
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import git

from auryn.core import generated
from auryn.core.local_source import log_skipped_generated
from auryn.core.source_filter import SourceFilter

logger = logging.getLogger(__name__)

_FETCH_REFSPEC = "+refs/heads/*:refs/heads/*"

DEFAULT_CACHE_ROOT = Path.home() / ".cache" / "auryn"


def cache_home() -> Path:
    """Root for all on-disk caches -- git clones (`repos/`) and the per-language parse caches. An
    explicit `cache_root` always wins; otherwise `$AURYN_CACHE_HOME`, else `~/.cache/auryn`. Resolved
    at CALL time (not import), so a parallel test worker -- or a daemon running concurrent ingests --
    can point its cache at an isolated directory via the env var with no import-order race, instead of
    several processes writing the one shared `~/.cache/auryn`."""
    env = os.environ.get("AURYN_CACHE_HOME")
    return Path(env) if env else DEFAULT_CACHE_ROOT


def parse_name_status(out: str, suffix: str) -> dict:
    """Parse `git diff --name-status -M` output into {modified, added, deleted, renamed} of
    `suffix` paths -- shared by GitRepoCache (commit-to-commit) and the working-tree source
    (worktree-vs-base). A rename crossing the suffix boundary degrades to add/delete of the
    matching side."""
    modified: list[str] = []
    added: list[str] = []
    deleted: list[str] = []
    renamed: list[tuple[str, str]] = []
    for line in out.splitlines():
        parts = line.split("\t")
        code = parts[0][0] if parts and parts[0] else ""
        if code == "R" and len(parts) >= 3:
            old_p, new_p = parts[1], parts[2]
            if old_p.endswith(suffix) and new_p.endswith(suffix):
                renamed.append((old_p, new_p))
            elif old_p.endswith(suffix):
                deleted.append(old_p)
            elif new_p.endswith(suffix):
                added.append(new_p)
        elif len(parts) >= 2 and parts[1].endswith(suffix):
            if code in ("A", "C"):
                added.append(parts[1])
            elif code == "D":
                deleted.append(parts[1])
            else:  # M, T (type change) -> re-process content
                modified.append(parts[1])
    return {"modified": modified, "added": added, "deleted": deleted, "renamed": renamed}


def repos_cache_dir(cache_root: Path | None = None) -> Path:
    """Where GitRepoCache's bare clones live -- shared by every git-based ingestor
    and by the viewer's read-only git-refs endpoint, so they all ever maintain only
    one local clone per repo, not one apiece. Lives here (next to GitRepoCache)
    rather than in any one ingestor so nothing in core has to import an ingestor to
    find it."""
    return (cache_root or cache_home()) / "repos"


class GitRepoCache:
    """Wraps a bare local clone of `repo_uri`, cached under a path derived from
    the URI itself (so no separate uri-to-path mapping needs to be persisted
    anywhere)."""

    def __init__(self, repo_uri: str, cache_root: Path, file_filter: SourceFilter | None = None) -> None:
        self._repo_uri = repo_uri
        self._path = Path(cache_root) / hashlib.sha256(repo_uri.encode()).hexdigest()
        self._filter = file_filter or SourceFilter()
        self._blob_cache: dict[str, list[tuple[str, str]]] = {}  # sha -> all allowed (path, blob_sha)

    def fetch(self) -> None:
        """Clones (first time) or fetches (otherwise) into the local bare cache."""
        if self._path.exists():
            repo = git.Repo(str(self._path))
            repo.remotes.origin.fetch()
            return

        self._path.parent.mkdir(parents=True, exist_ok=True)
        repo = git.Repo.clone_from(self._repo_uri, str(self._path), bare=True)
        with repo.config_writer() as cw:
            cw.set_value('remote "origin"', "fetch", _FETCH_REFSPEC)

    def default_ref(self) -> str:
        """The remote's *current* default branch name -- queried live via
        ls-remote rather than the local clone's own HEAD, which only reflects the
        default branch as of whenever this repo was first cloned (see module
        docstring)."""
        repo = git.Repo(str(self._path))
        output = repo.git.ls_remote("--symref", self._repo_uri, "HEAD")
        symref_line = next(line for line in output.splitlines() if line.startswith("ref:"))
        ref = symref_line.split()[1]
        return ref.removeprefix("refs/heads/")

    def resolve_ref(self, ref: str | None) -> str:
        """Resolves a branch name, tag, or raw sha to a full commit sha.
        `ref=None` resolves the remote's current default branch. Raises
        ValueError (not GitPython's own BadName) for an unresolvable ref, so
        callers (the CLI, the viewer) don't need to know GitPython's exception
        types to handle "the user typed a ref that doesn't exist" gracefully."""
        repo = git.Repo(str(self._path))
        target = ref or self.default_ref()
        try:
            return repo.commit(target).hexsha
        except git.BadName as e:
            raise ValueError(f"{target!r} does not match any branch, tag, or commit in this repository") from e

    def list_refs(self) -> dict:
        """Every branch and tag currently on the remote, plus which branch is the
        remote's current default -- fetches first, so this reflects what's
        actually on the remote right now (e.g. a branch pushed five minutes ago),
        not just whatever this cache last had. Each list is newest-commit-first."""
        self.fetch()
        repo = git.Repo(str(self._path))

        def by_recency(names: list[str]) -> list[str]:
            return sorted(names, key=lambda name: repo.commit(name).committed_date, reverse=True)

        return {
            "branches": by_recency([h.name for h in repo.heads]),
            "tags": by_recency([t.name for t in repo.tags]),
            "default_branch": self.default_ref(),
        }

    def _all_files(self, sha: str) -> list[tuple[str, str]]:
        """Every allowed (path, blob_sha) at `sha`, computed by ONE tree traversal and cached.
        The whole tree walk (+ user filter + the generated/vendored head-read) is suffix- and
        excluded-dir-independent, so it's done once per sha rather than once per language-suffix --
        list_files() below is then a cheap in-memory filter. This is the difference between a
        commit resync spending ~1.3s re-walking the tree ~45× (once per registered language's
        suffixes) and doing it once.

        INVALIDATION: the cache key is a resolved COMMIT SHA, which is content-addressed and
        immutable -- a file add/delete/rename/edit is a different commit and therefore a different
        sha, i.e. a cache miss and a fresh traversal. So there is nothing to invalidate; there is no
        path where a given sha's tree can change under a cached entry. (Callers always pass a
        resolved sha, never a mutable ref/branch -- the ingest resolves the ref up front -- which is
        what makes this safe; do not key this by a branch name.)"""
        cached = self._blob_cache.get(sha)
        if cached is not None:
            return cached
        repo = git.Repo(str(self._path))
        commit = repo.commit(sha)
        detect_generated = generated.detection_enabled()
        files: list[tuple[str, str]] = []
        skipped: list[tuple[str, str]] = []
        for item in commit.tree.traverse():
            if item.type != "blob":
                continue
            if not self._filter.allows(item.path):  # user include/exclude (dirs + suffixes)
                continue
            if detect_generated:
                # Read only the head -- a generator banner is at the top -- so this stays cheap.
                head = item.data_stream.read(generated.HEAD_BYTES)
                reason = generated.is_generated(item.path, head)
                if reason:
                    skipped.append((item.path, reason))
                    continue
            files.append((item.path, item.hexsha))
        if skipped:
            log_skipped_generated(sorted(skipped))
        self._blob_cache[sha] = files
        return files

    def list_files(self, sha: str, suffix: str, excluded_dir_names: set[str]) -> list[tuple[str, str]]:
        """Every blob at `sha` whose path ends with `suffix`, excluding anything under a directory
        named in `excluded_dir_names` -- returns (relative_path, blob_sha) pairs. A cheap suffix/dir
        filter over the cached one-time tree traversal (`_all_files`)."""
        return [
            (path, blob_sha) for path, blob_sha in self._all_files(sha)
            if path.endswith(suffix) and not (excluded_dir_names & set(path.split("/")[:-1]))
        ]

    def read_blob(self, blob_sha: str) -> bytes:
        repo = git.Repo(str(self._path))
        return repo.odb.stream(bytes.fromhex(blob_sha)).read()

    def diff_changed_files(self, old_sha: str, new_sha: str, suffix: str) -> dict:
        """What changed between two commits, restricted to `suffix` files, as
        {modified, added, deleted, renamed:[(old,new)]} of repo-relative paths -- the
        changed set an incremental resync starts from (ADR 0002). Rename-aware (`-M`); a
        rename that crosses the suffix boundary degrades to add/delete of the matching side.
        Raises git.GitCommandError if `old_sha` isn't reachable (e.g. a shallow clone or a
        gc'd/force-pushed commit) -- the caller treats that as "fall back to a full resync".
        Deliberately NOT filtered by the source's dir/suffix filter: the incremental
        orchestrator intersects these paths with the live file listing (which is filtered),
        so an excluded or already-gone path simply drops out there."""
        repo = git.Repo(str(self._path))
        out = repo.git.diff("-M", "--name-status", old_sha, new_sha)
        return parse_name_status(out, suffix)

    def materialize(self, sha: str, dest_dir: Path) -> None:
        """Extract the full working tree at `sha` into `dest_dir` -- for tools that
        need real files on disk rather than blob streams (e.g. the Roslyn extractor,
        which builds a Compilation over the source tree). Uses `git archive` so no
        working tree or checkout is created in the bare cache itself; the tar is
        streamed through a temp file rather than held in memory, since a materialized
        1M+ LOC repo is large."""
        import tarfile
        import tempfile

        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        repo = git.Repo(str(self._path))
        with tempfile.TemporaryFile() as archive:
            repo.archive(archive, treeish=sha)
            archive.seek(0)
            with tarfile.open(fileobj=archive) as tar:
                tar.extractall(dest, filter="data")

    @contextmanager
    def working_tree(self, sha: str) -> Iterator[Path]:
        """Materialize the tree at `sha` into a fresh temp directory, yield its path,
        and clean it up on exit -- the uniform "give me a real directory to analyze"
        method the Roslyn engine drives. LocalDirectorySource mirrors this method by
        yielding its own directory directly (no copy), so a source-consuming caller
        (e.g. csharp/git_sync._roslyn_sync_at) works over either source unchanged."""
        import tempfile

        with tempfile.TemporaryDirectory() as workdir:
            self.materialize(sha, Path(workdir))
            yield Path(workdir)
