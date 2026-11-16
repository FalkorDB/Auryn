"""Unit tests for LocalDirectorySource -- the directory-backed stand-in that lets the
git ingestion pipeline run over a plain folder. Pure filesystem, no graph/git needed.
It must present the same list_files/read_blob/working_tree surface as GitRepoCache so
core/ingest.py can treat the two interchangeably. (The one WorkingTreeSource test at the
end does build a real git repo: its change detection is `git diff`.)
"""

from __future__ import annotations

import hashlib

import git
import pytest

from auryn.core.local_source import LocalDirectorySource, WorkingTreeSource

_SHA = "deadbeef"  # the `sha` arg is ignored by a directory source -- any value works


def _build(root, files: dict[str, str]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def test_list_files_filters_by_suffix(tmp_path):
    _build(tmp_path, {"a/Widget.cs": "x", "b/Other.cs": "y", "notes.txt": "z"})
    source = LocalDirectorySource(tmp_path)

    cs = source.list_files(_SHA, ".cs", set())

    assert {rel for rel, _ in cs} == {"a/Widget.cs", "b/Other.cs"}
    # Paths are relative-posix, matching GitRepoCache.list_files.
    assert all("/" in rel or not rel.startswith("/") for rel, _ in cs)


def test_list_files_excludes_named_dirs(tmp_path):
    _build(tmp_path, {"src/A.cs": "x", "obj/Generated.cs": "y", "bin/B.cs": "z"})
    source = LocalDirectorySource(tmp_path)

    cs = source.list_files(_SHA, ".cs", {"bin", "obj"})

    assert {rel for rel, _ in cs} == {"src/A.cs"}


def test_cmake_dependencies_are_skipped_unless_opted_in(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("AURYN_INCLUDE_GENERATED", raising=False)
    legacy = "deps/RediSearch/bin/linux-x64-release/search-community/_deps/boost-src/legacy.py"
    _build(tmp_path, {"src/app.py": "pass\n", legacy: "print 'Python 2'\n"})
    with caplog.at_level("INFO", logger="auryn.core.local_source"):
        source = LocalDirectorySource(tmp_path)
        assert {rel for rel, _ in source.list_files(_SHA, ".py", set())} == {"src/app.py"}
    assert legacy in caplog.text
    assert "vendored dir" in caplog.text
    before = source.tree_sha()
    (tmp_path / legacy).write_text("print 'changed dependency'\n")
    assert LocalDirectorySource(tmp_path).tree_sha() == before

    monkeypatch.setenv("AURYN_INCLUDE_GENERATED", "1")
    assert {rel for rel, _ in LocalDirectorySource(tmp_path).list_files(_SHA, ".py", set())} == {
        "src/app.py", legacy,
    }


def test_git_internals_are_never_listed(tmp_path):
    _build(tmp_path, {"src/A.cs": "x", ".git/objects/ab/cd.cs": "junk", ".git/config": "[core]"})
    source = LocalDirectorySource(tmp_path)

    assert {rel for rel, _ in source.list_files(_SHA, ".cs", set())} == {"src/A.cs"}
    # Nothing under .git leaks into any listing.
    assert source.list_files(_SHA, "config", set()) == []


def test_content_hash_is_the_blob_key_and_read_blob_round_trips(tmp_path):
    _build(tmp_path, {"src/A.cs": "class A {}"})
    source = LocalDirectorySource(tmp_path)

    (rel, content_hash), = source.list_files(_SHA, ".cs", set())
    assert content_hash == hashlib.sha256(b"class A {}").hexdigest()
    assert source.read_blob(content_hash) == b"class A {}"


def test_identical_content_shares_one_key(tmp_path):
    _build(tmp_path, {"a/Dup.cs": "same", "b/Dup.cs": "same"})
    source = LocalDirectorySource(tmp_path)

    hashes = {content_hash for _, content_hash in source.list_files(_SHA, ".cs", set())}
    assert len(hashes) == 1  # same bytes -> same blob sha, exactly like git
    assert source.read_blob(next(iter(hashes))) == b"same"


def test_read_blob_unknown_hash_raises(tmp_path):
    _build(tmp_path, {"a.cs": "x"})
    source = LocalDirectorySource(tmp_path)

    try:
        source.read_blob("0" * 64)
    except KeyError:
        pass
    else:  # pragma: no cover - guard
        raise AssertionError("expected KeyError for an unknown content hash")


def test_working_tree_yields_the_directory_itself(tmp_path):
    _build(tmp_path, {"src/A.cs": "x"})
    source = LocalDirectorySource(tmp_path)

    with source.working_tree(_SHA) as workdir:
        assert workdir == tmp_path.resolve()
        assert (workdir / "src" / "A.cs").read_text() == "x"


def test_tree_sha_is_stable_across_instances(tmp_path):
    _build(tmp_path, {"a.cs": "x", "b/c.cs": "y"})

    first = LocalDirectorySource(tmp_path).tree_sha()
    second = LocalDirectorySource(tmp_path).tree_sha()
    assert first == second


def test_tree_sha_changes_when_file_content_changes(tmp_path):
    _build(tmp_path, {"a.cs": "x"})
    before = LocalDirectorySource(tmp_path).tree_sha()

    (tmp_path / "a.cs").write_text("changed")
    after = LocalDirectorySource(tmp_path).tree_sha()

    assert before != after


def test_tree_sha_changes_when_a_file_is_added_or_removed(tmp_path):
    _build(tmp_path, {"a.cs": "x"})
    base = LocalDirectorySource(tmp_path).tree_sha()

    (tmp_path / "b.cs").write_text("y")
    with_added = LocalDirectorySource(tmp_path).tree_sha()
    assert with_added != base

    (tmp_path / "b.cs").unlink()
    back = LocalDirectorySource(tmp_path).tree_sha()
    assert back == base  # removing the addition restores the original tree hash


def test_tree_sha_changes_when_a_file_is_renamed(tmp_path):
    _build(tmp_path, {"a.cs": "x"})
    before = LocalDirectorySource(tmp_path).tree_sha()

    (tmp_path / "a.cs").rename(tmp_path / "renamed.cs")
    after = LocalDirectorySource(tmp_path).tree_sha()

    # Same bytes, different path -> the tree changed (path is part of the hash), even
    # though the individual blob key for that content is unchanged.
    assert before != after


# -- WorkingTreeSource.changed_since (a real git working tree) ---------------------------------


def test_changed_since_never_runs_the_base_commit_as_an_option(tmp_path):
    """`base_commit` is the commit read back from the graph: a value there that starts with `-`
    must fail as a bad revision (-> the caller's full-resync fallback), not run as a git option."""
    root = tmp_path / "repo"
    repo = git.Repo.init(str(root))
    with repo.config_writer() as cw:
        cw.set_value("user", "email", "test@example.com")
        cw.set_value("user", "name", "Test")
    _build(root, {"a.py": "x = 1\n"})
    repo.index.add(["a.py"])
    repo.index.commit("first commit")
    (root / "a.py").write_text("x = 2\n")

    victim = tmp_path / "victim.txt"
    with pytest.raises(git.GitCommandError):
        WorkingTreeSource(root).changed_since(f"--output={victim}", ".py")
    assert not victim.exists()
    assert WorkingTreeSource(root).changed_since(repo.head.commit.hexsha, ".py")["modified"] == ["a.py"]
