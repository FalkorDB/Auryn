"""Unit tests for GitRepoCache -- a real local git repo built with GitPython in
tmp_path, no network needed. Covers ref resolution (branch/tag/sha/default),
tree listing, blob reading, and excluded-directory filtering."""

from __future__ import annotations

import git
import pytest

from auryn.core.git_source import GitRepoCache


@pytest.fixture
def origin_repo(tmp_path):
    """A small real git repo: two commits on main (including a nested file and a
    file under an excluded "obj" directory), plus a divergent "feature" branch
    and a tag -- enough to exercise every resolution path GitRepoCache supports."""
    origin_path = tmp_path / "origin"
    repo = git.Repo.init(str(origin_path), initial_branch="main")
    with repo.config_writer() as cw:
        cw.set_value("user", "email", "test@example.com")
        cw.set_value("user", "name", "Test")

    (origin_path / "a.cs").write_text("class A {}")
    repo.index.add(["a.cs"])
    repo.index.commit("first commit")

    (origin_path / "sub").mkdir()
    (origin_path / "sub" / "b.cs").write_text("class B {}")
    (origin_path / "obj").mkdir()
    (origin_path / "obj" / "Generated.cs").write_text("class ShouldBeExcluded {}")
    repo.index.add(["sub/b.cs", "obj/Generated.cs"])
    repo.index.commit("second commit")
    repo.create_tag("v1.0")

    repo.git.checkout("-b", "feature")
    (origin_path / "c.cs").write_text("class C {}")
    repo.index.add(["c.cs"])
    repo.index.commit("feature commit")
    repo.git.checkout("main")

    return origin_path


@pytest.fixture
def cache(origin_repo, tmp_path):
    c = GitRepoCache(str(origin_repo), tmp_path / "cache")
    c.fetch()
    return c


def test_default_ref_resolves_to_the_remotes_current_default_branch(cache):
    assert cache.default_ref() == "main"


def test_resolve_ref_none_resolves_to_default_branch_tip(cache, origin_repo):
    origin = git.Repo(str(origin_repo))
    assert cache.resolve_ref(None) == origin.commit("main").hexsha


def test_resolve_ref_accepts_a_branch_name(cache, origin_repo):
    origin = git.Repo(str(origin_repo))
    assert cache.resolve_ref("feature") == origin.commit("feature").hexsha


def test_resolve_ref_accepts_a_tag(cache, origin_repo):
    origin = git.Repo(str(origin_repo))
    assert cache.resolve_ref("v1.0") == origin.commit("v1.0").hexsha


def test_resolve_ref_accepts_a_raw_sha(cache, origin_repo):
    origin = git.Repo(str(origin_repo))
    sha = origin.commit("main").hexsha
    assert cache.resolve_ref(sha) == sha


def test_resolve_ref_raises_value_error_not_gitpythons_own_exception_for_an_unknown_ref(cache):
    with pytest.raises(ValueError, match="not-a-real-ref"):
        cache.resolve_ref("not-a-real-ref")


def test_fetch_picks_up_new_commits_pushed_to_the_default_branch(cache, origin_repo):
    origin = git.Repo(str(origin_repo))
    (origin_repo / "a.cs").write_text("class A { /* changed */ }")
    origin.index.add(["a.cs"])
    origin.index.commit("third commit")

    cache.fetch()

    assert cache.resolve_ref(None) == origin.commit("main").hexsha


def test_default_ref_reflects_a_remote_default_branch_change_not_the_stale_local_head(cache, origin_repo):
    # The bare clone's own HEAD only reflects the default branch as of clone time
    # -- default_ref() must re-resolve live against the remote, not trust it.
    origin = git.Repo(str(origin_repo))
    origin.git.symbolic_ref("HEAD", "refs/heads/feature")
    try:
        assert cache.default_ref() == "feature"
    finally:
        origin.git.symbolic_ref("HEAD", "refs/heads/main")


def test_list_refs_returns_every_branch_and_tag_and_the_default(cache):
    refs = cache.list_refs()

    assert set(refs["branches"]) == {"main", "feature"}
    assert refs["tags"] == ["v1.0"]
    assert refs["default_branch"] == "main"


def test_list_refs_picks_up_a_branch_pushed_after_the_last_fetch(cache, origin_repo):
    origin = git.Repo(str(origin_repo))
    origin.git.checkout("-b", "brand-new-branch")
    origin.git.checkout("main")

    refs = cache.list_refs()

    assert "brand-new-branch" in refs["branches"]


def test_list_files_filters_by_suffix_and_excludes_configured_directories(cache):
    sha = cache.resolve_ref("main")
    files = dict(cache.list_files(sha, ".cs", {"obj"}))

    assert set(files) == {"a.cs", "sub/b.cs"}


def test_list_files_at_an_earlier_commit_reflects_that_commits_own_tree(cache, origin_repo):
    origin = git.Repo(str(origin_repo))
    first_commit_sha = list(origin.iter_commits("main"))[-1].hexsha

    files = dict(cache.list_files(first_commit_sha, ".cs", {"obj"}))
    assert set(files) == {"a.cs"}


def test_read_blob_returns_the_exact_content(cache):
    sha = cache.resolve_ref("main")
    files = dict(cache.list_files(sha, ".cs", {"obj"}))
    content = cache.read_blob(files["sub/b.cs"])
    assert content == b"class B {}"
