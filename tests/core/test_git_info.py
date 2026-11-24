"""Git-information ingestion and lifecycle behavior against embedded FalkorDB."""

from __future__ import annotations

import shutil

import git
import pytest

from auryn.core import git_info, ingest
from auryn.core.app_config import set_git_info_enabled
from auryn.core.graph_client import GraphWriter
from auryn.core.graph_model import GraphEdge, GraphNode


def _make_repo(root):
    repo = git.Repo.init(str(root), initial_branch="main")
    with repo.config_writer() as config:
        config.set_value("user", "name", "Ada Example")
        config.set_value("user", "email", "ada@example.org")
    (root / "main.py").write_text("def run():\n    return 1\n")
    repo.index.add(["main.py"])
    repo.index.commit("initial commit")
    repo.create_remote("origin", "https://github.com/acme/one.git")
    return repo


def _count(graph, label):
    return graph.ro_query(f"MATCH (n:{label}) RETURN count(n)").result_set[0][0]


def test_full_history_is_incremental_and_remote_change_replaces_subgraph(
    graph, tmp_path, monkeypatch
):
    repo = _make_repo(tmp_path / "checkout")
    writer = GraphWriter(graph)
    monkeypatch.setattr(git_info, "_verify_public_github", lambda *_: None)

    def remote_data(owner, name, repo_id, default_branch=None):
        node_id = f"{repo_id}/issue/7"
        node = GraphNode("GitIssue", {"id": node_id},
                         {"id": node_id, "number": 7, "title": f"{owner}/{name}"})
        edge = GraphEdge("HAS_ISSUE", "GitRepository", {"id": repo_id}, "GitIssue", {"id": node_id})
        return [node], [edge]

    remote_calls = []

    def counted_remote_data(*args):
        remote_calls.append(args)
        return remote_data(*args)

    monkeypatch.setattr(git_info, "_github_metadata", counted_remote_data)
    monkeypatch.setattr(git_info, "_github_checks", lambda *_: ([], []))
    monkeypatch.setattr(git_info, "_github_commit_logins", lambda *a, **k: {})
    writer.merge_nodes([
        GraphNode("File", {"id": "python://checkout/file/main.py"},
                  {"id": "python://checkout/file/main.py", "path": "main.py"}),
        GraphNode("File", {"id": "python://checkout/file/src/main.py"},
                  {"id": "python://checkout/file/src/main.py", "path": "src/main.py"}),
    ])
    first = git_info.sync_git_info(writer, repo.working_tree_dir)
    assert first["commits"] == 1
    assert first["issues"] == 1
    assert _count(graph, "GitCommit") == 1
    changes = graph.ro_query(
        "MATCH (c:GitCommit)-[e:CHANGED]->(f:File) RETURN f.path, e.change_type"
    ).result_set
    assert changes == [["main.py", "A"]]
    assert _count(graph, "GitUser") == 1
    assert _count(graph, "GitIssue") == 1
    assert _count(graph, "GitCheckout") == 1

    # Same history is a no-op; the stored branch tips let sync avoid re-writing old commits.
    second = git_info.sync_git_info(writer, repo.working_tree_dir)
    assert second["commits"] == 0
    assert len(remote_calls) == 1  # automatic remote refresh is rate-limited to once per day
    assert _count(graph, "GitCommit") == 1

    (tmp_path / "checkout" / "main.py").write_text("def run():\n    return 2\n")
    repo.index.add(["main.py"])
    repo.index.commit("second commit")
    third = git_info.sync_git_info(writer, repo.working_tree_dir)
    assert third["commits"] == 1
    assert _count(graph, "GitCommit") == 2
    modified = graph.ro_query(
        "MATCH (c:GitCommit {summary:'second commit'})-[e:CHANGED]->(f:File) "
        "RETURN f.path, e.change_type"
    ).result_set
    assert modified == [["main.py", "M"]]

    (tmp_path / "checkout" / "src").mkdir()
    (tmp_path / "checkout" / "main.py").rename(tmp_path / "checkout" / "src" / "main.py")
    repo.git.add(A=True)
    repo.index.commit("rename file")
    renamed = git_info.sync_git_info(writer, repo.working_tree_dir)
    assert renamed["commits"] == 1
    rename_rows = graph.ro_query(
        "MATCH (:GitCommit {summary:'rename file'})-[e:CHANGED]->(f:File) "
        "RETURN f.path, e.change_type ORDER BY f.path"
    ).result_set
    assert rename_rows == [["main.py", "D"], ["src/main.py", "A"]]

    graph.query("MATCH (f:File {path:'src/main.py'}) DETACH DELETE f")
    (tmp_path / "checkout" / "src" / "main.py").unlink()
    repo.git.add(A=True)
    repo.index.commit("delete file")
    deleted = git_info.sync_git_info(writer, repo.working_tree_dir)
    assert deleted["commits"] == 1
    delete_rows = graph.ro_query(
        "MATCH (:GitCommit {summary:'delete file'})-[e:CHANGED]->(f:File) RETURN f.path"
    ).result_set
    assert delete_rows == []
    changed_files = graph.ro_query(
        "MATCH (c:GitCommit {summary:'delete file'}) RETURN c.changed_files"
    ).result_set[0][0]
    assert git_info.json.loads(changed_files)[0]["path"] == "src/main.py"

    # Changing origin moves this checkout to a new provenance root and removes the old
    # root only after its last checkout reference is gone.
    repo.remotes.origin.set_url("https://github.com/acme/two.git")
    git_info.sync_git_info(writer, repo.working_tree_dir, force_remote=True)
    old_root = graph.ro_query("MATCH (r:GitRepository {id: $id}) RETURN count(r)",
                              {"id": "github://acme/one"}).result_set[0][0]
    new_root = graph.ro_query("MATCH (r:GitRepository {id: $id}) RETURN count(r)",
                              {"id": "github://acme/two"}).result_set[0][0]
    assert old_root == 0
    assert new_root == 1
    assert _count(graph, "GitIssue") == 1


def test_changes_link_to_existing_code_file_instead_of_duplicate_git_file(graph, tmp_path, monkeypatch):
    repo = _make_repo(tmp_path / "checkout")
    writer = GraphWriter(graph)
    monkeypatch.setattr(git_info, "_verify_public_github", lambda *_: None)
    monkeypatch.setattr(git_info, "_github_metadata", lambda *_: ([], []))
    monkeypatch.setattr(git_info, "_github_checks", lambda *_: ([], []))
    monkeypatch.setattr(git_info, "_github_commit_logins", lambda *a, **k: {})
    writer.merge_node(GraphNode("File", {"id": "python://demo/file/main.py"},
                                {"id": "python://demo/file/main.py", "path": "main.py"}))

    git_info.sync_git_info(writer, repo.working_tree_dir)

    changed = graph.ro_query(
        "MATCH (:GitCommit)-[:CHANGED]->(f:File) RETURN f.id"
    ).result_set
    assert changed == [["python://demo/file/main.py"]]
    assert _count(graph, "GitFile") == 0


def test_changed_edges_skipped_when_only_another_repos_project_is_present(graph, tmp_path, monkeypatch):
    # A graph whose one Project belongs to a DIFFERENT repo must NOT get this repo's commits
    # cross-linked to that repo's same-named files (the scoped match fails; the all-files fallback is
    # unsafe here because a single project doesn't prove it is this repo).
    repo = _make_repo(tmp_path / "checkout")
    writer = GraphWriter(graph)
    monkeypatch.setattr(git_info, "_verify_public_github", lambda *_: None)
    monkeypatch.setattr(git_info, "_github_metadata", lambda *_: ([], []))
    monkeypatch.setattr(git_info, "_github_checks", lambda *_: ([], []))
    monkeypatch.setattr(git_info, "_github_commit_logins", lambda *a, **k: {})
    writer.merge_node(GraphNode("Project", {"id": "proj-other"},
                                {"id": "proj-other", "repo_uri": "https://github.com/other/repo"}))
    writer.merge_node(GraphNode("File", {"id": "other://file/main.py"},
                                {"id": "other://file/main.py", "path": "main.py"}))
    graph.query("MATCH (p:Project {id:'proj-other'}), (f:File {id:'other://file/main.py'}) "
                "CREATE (p)-[:HAS_FILE]->(f)")

    git_info.sync_git_info(writer, repo.working_tree_dir)

    assert graph.ro_query("MATCH (:GitCommit)-[:CHANGED]->(:File) RETURN count(*)").result_set[0][0] == 0


def test_changed_file_without_code_node_has_no_duplicate_file_entity(graph, tmp_path, monkeypatch):
    repo = _make_repo(tmp_path / "checkout")
    writer = GraphWriter(graph)
    monkeypatch.setattr(git_info, "_verify_public_github", lambda *_: None)
    monkeypatch.setattr(git_info, "_github_metadata", lambda *_: ([], []))
    monkeypatch.setattr(git_info, "_github_checks", lambda *_: ([], []))
    monkeypatch.setattr(git_info, "_github_commit_logins", lambda *a, **k: {})

    git_info.sync_git_info(writer, repo.working_tree_dir)

    assert _count(graph, "GitFile") == 0
    assert graph.ro_query("MATCH (:GitCommit)-[:CHANGED]->(f:File) RETURN f.id").result_set == []


def test_changes_link_to_python_and_typescript_modules(graph, tmp_path, monkeypatch):
    # Python and TypeScript model a source file as a `Module` (Python `file_path`, TypeScript `path`),
    # not a `File` -- so a commit that touches one must reach that Module, found through its Project.
    root = tmp_path / "checkout"
    repo = _make_repo(root)
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "core.py").write_text("def helper():\n    return 2\n")
    (root / "web").mkdir()
    (root / "web" / "app.ts").write_text("export function render(): number {\n  return 3;\n}\n")
    repo.git.add(A=True)
    repo.index.commit("add package and web app")
    writer = GraphWriter(graph)
    monkeypatch.setattr(git_info, "_verify_public_github", lambda *_: None)
    monkeypatch.setattr(git_info, "_github_metadata", lambda *_: ([], []))
    monkeypatch.setattr(git_info, "_github_checks", lambda *_: ([], []))
    monkeypatch.setattr(git_info, "_github_commit_logins", lambda *a, **k: {})

    set_git_info_enabled(True)
    ingest.ingest_directory(writer, str(root), project_name="Checkout", deep="off", auto_install=False)

    rows = graph.ro_query(
        "MATCH (c:GitCommit)-[e:CHANGED]->(m:Module) "
        "RETURN c.summary, coalesce(m.file_path, m.path) AS path, e.change_type ORDER BY c.summary, path"
    ).result_set
    assert rows == [
        ["add package and web app", "pkg/core.py", "A"],
        ["add package and web app", "web/app.ts", "A"],
        ["initial commit", "main.py", "A"],
    ]


def test_remote_change_keeps_data_while_another_checkout_still_uses_old_repo(
    graph, tmp_path, monkeypatch
):
    first_repo = _make_repo(tmp_path / "first")
    second_path = tmp_path / "second"
    second_repo = git.Repo.clone_from(str(tmp_path / "first"), str(second_path))
    second_repo.remotes.origin.set_url("https://github.com/acme/one.git")
    writer = GraphWriter(graph)
    monkeypatch.setattr(git_info, "_verify_public_github", lambda *_: None)
    monkeypatch.setattr(git_info, "_github_metadata", lambda *_: ([], []))
    monkeypatch.setattr(git_info, "_github_checks", lambda *_: ([], []))
    monkeypatch.setattr(git_info, "_github_commit_logins", lambda *a, **k: {})

    git_info.sync_git_info(writer, first_repo.working_tree_dir)
    git_info.sync_git_info(writer, second_repo.working_tree_dir)
    assert _count(graph, "GitCheckout") == 2

    first_repo.remotes.origin.set_url("https://github.com/acme/two.git")
    git_info.sync_git_info(writer, first_repo.working_tree_dir, force_remote=True)
    roots = {row[0] for row in graph.ro_query("MATCH (r:GitRepository) RETURN r.id").result_set}
    assert roots == {"github://acme/one", "github://acme/two"}
    assert _count(graph, "GitCommit") == 2  # both repositories retain their own commit nodes


def test_github_mapping_captures_issues_prs_releases_and_users(monkeypatch):
    responses = {
        "repos/acme/demo/issues?state=all&sort=created&direction=desc&per_page=100": [
            {"number": 4, "title": "Bug", "body": "Details", "state": "open",
             "html_url": "https://github.com/acme/demo/issues/4", "user": {"login": "reporter"},
             "labels": [{"name": "bug"}]},
            {"number": 5, "title": "Change", "body": "", "state": "closed",
             "pull_request": {}, "user": {"login": "author"}, "labels": []},
            {"number": 6, "title": "Related work", "body": "", "state": "open",
             "pull_request": {}, "user": {"login": "contributor"}, "labels": []},
            {"number": 7, "title": "Other target", "body": "", "state": "closed",
             "pull_request": {}, "user": {"login": "author"}, "labels": []},
        ],
        "repos/acme/demo/pulls?state=all&sort=created&direction=desc&per_page=100": [
            {"number": 5, "title": "Change", "body": "Fixes #4", "state": "closed", "merged": True,
             "merged_at": "2025-01-02T00:00:00Z", "draft": False,
             "user": {"login": "author"}, "head": {"sha": "a" * 40},
             "base": {"sha": "b" * 40, "ref": "main"}, "merge_commit_sha": "c" * 40},
            {"number": 6, "title": "Related work", "body": "See #4 and acme/other#4", "state": "open",
             "merged": False, "user": {"login": "contributor"}, "head": {"sha": "d" * 40},
             "base": {"sha": "b" * 40}},
            {"number": 7, "title": "Other target", "body": "Fixes #4", "state": "closed",
             "merged": True, "user": {"login": "author"}, "head": {"sha": "e" * 40},
             "base": {"sha": "f" * 40, "ref": "release"}},
        ],
        "repos/acme/demo/releases?per_page=100": [
            {"id": 10, "tag_name": "v1", "name": "Version 1", "body": "Release notes",
             "draft": False, "prerelease": False, "published_at": "2025-01-02T00:00:00Z",
             "html_url": "https://github.com/acme/demo/releases/10", "author": {"login": "maintainer"}},
        ],
        "repos/acme/demo/tags?per_page=100": [
            {"name": "v1", "commit": {"sha": "c" * 40}},
        ],
    }
    monkeypatch.setattr(git_info, "_api_pages", lambda path: responses[path])
    nodes, edges = git_info._github_metadata("acme", "demo", "github://acme/demo", "main")
    by_label = {}
    for node in nodes:
        by_label.setdefault(node.label, []).append(node)
    assert len(by_label["GitIssue"]) == 1
    assert len(by_label["GitPullRequest"]) == 6  # issue list plus PR detail merge on stable ID
    assert len(by_label["GitRelease"]) == 1
    assert len(by_label["GitTag"]) == 1
    assert {node.properties["name"] for node in by_label["GitUser"]} == {
        "reporter", "author", "contributor", "maintainer"
    }
    assert any(edge.rel_type == "MERGED_AS" for edge in edges)
    triples = {(edge.rel_type, edge.from_key["id"], edge.to_key["id"]) for edge in edges}
    assert ("OPENED_BY", "github://acme/demo/issue/4", "github://acme/demo/user/github%3Areporter") in triples
    assert ("OPENED_BY", "github://acme/demo/pull/5", "github://acme/demo/user/github%3Aauthor") in triples
    assert ("CREATED_BY", "github://acme/demo/release/10", "github://acme/demo/user/github%3Amaintainer") in triples
    assert ("REFERENCES_ISSUE", "github://acme/demo/pull/5", "github://acme/demo/issue/4") in triples
    assert ("CLOSES_ISSUE", "github://acme/demo/pull/5", "github://acme/demo/issue/4") in triples
    assert ("REFERENCES_ISSUE", "github://acme/demo/pull/6", "github://acme/demo/issue/4") in triples
    assert not any(rel == "CLOSES_ISSUE" and src.endswith("pull/6") for rel, src, _ in triples)
    assert ("REFERENCES_ISSUE", "github://acme/demo/pull/7", "github://acme/demo/issue/4") in triples
    assert not any(rel == "CLOSES_ISSUE" and src.endswith("pull/7") for rel, src, _ in triples)


def _meta_responses(*, issues=(), pulls=(), releases=(), tags=()):
    return {
        "repos/acme/demo/issues?state=all&sort=created&direction=desc&per_page=100": list(issues),
        "repos/acme/demo/pulls?state=all&sort=created&direction=desc&per_page=100": list(pulls),
        "repos/acme/demo/releases?per_page=100": list(releases),
        "repos/acme/demo/tags?per_page=100": list(tags),
    }


def test_pr_merged_derived_from_merged_at_fires_closes_issue_and_merged_as(monkeypatch):
    # The pulls LIST endpoint omits the `merged` boolean but returns `merged_at`; a merged PR must
    # still close its issue and record MERGED_AS.
    responses = _meta_responses(
        issues=[{"number": 4, "title": "Bug", "body": "", "state": "closed",
                 "user": {"login": "r"}, "labels": []}],
        pulls=[{"number": 5, "title": "Fix", "body": "Fixes #4", "state": "closed",
                "merged_at": "2025-01-02T00:00:00Z",  # NO `merged` key (the list-endpoint reality)
                "user": {"login": "a"}, "head": {"sha": "a" * 40},
                "base": {"sha": "b" * 40, "ref": "main"}, "merge_commit_sha": "c" * 40}],
    )
    monkeypatch.setattr(git_info, "_api_pages", lambda path: responses[path])
    nodes, edges = git_info._github_metadata("acme", "demo", "github://acme/demo", "main")
    pr = next(n for n in nodes if n.label == "GitPullRequest")
    assert pr.properties["merged"] is True
    triples = {(e.rel_type, e.from_key["id"], e.to_key["id"]) for e in edges}
    assert ("CLOSES_ISSUE", "github://acme/demo/pull/5", "github://acme/demo/issue/4") in triples
    assert ("MERGED_AS", "github://acme/demo/pull/5", f"github://acme/demo/commit/{'c' * 40}") in triples


def test_open_pr_with_test_merge_sha_is_not_marked_merged(monkeypatch):
    # GitHub sets merge_commit_sha to a throwaway test-merge SHA for an OPEN mergeable PR. That must
    # not be mistaken for a merge.
    responses = _meta_responses(
        pulls=[{"number": 8, "title": "WIP", "body": "", "state": "open",  # no merged / merged_at
                "user": {"login": "a"}, "head": {"sha": "h" * 40},
                "base": {"sha": "b" * 40, "ref": "main"}, "merge_commit_sha": "t" * 40}],
    )
    monkeypatch.setattr(git_info, "_api_pages", lambda path: responses[path])
    nodes, edges = git_info._github_metadata("acme", "demo", "github://acme/demo", "main")
    pr = next(n for n in nodes if n.label == "GitPullRequest")
    assert pr.properties["merged"] is False
    assert not any(e.rel_type == "MERGED_AS" for e in edges)


def test_draft_releases_are_not_collected(monkeypatch):
    responses = _meta_responses(releases=[
        {"id": 1, "tag_name": "v9", "name": "Draft", "draft": True, "prerelease": False},
        {"id": 2, "tag_name": "v8", "name": "Published", "draft": False, "prerelease": False},
    ])
    monkeypatch.setattr(git_info, "_api_pages", lambda path: responses[path])
    nodes, _ = git_info._github_metadata("acme", "demo", "github://acme/demo", "main")
    assert {n.properties["tag_name"] for n in nodes if n.label == "GitRelease"} == {"v8"}


def test_github_owner_repo_requires_exact_host():
    f = git_info._github_owner_repo
    assert f("https://github.com/acme/demo.git") == ("acme", "demo")
    assert f("git@github.com:acme/demo.git") == ("acme", "demo")
    assert f("ssh://git@github.com/acme/demo") == ("acme", "demo")
    assert f("https://notgithub.com/acme/demo.git") is None       # lookalike host
    assert f("https://github.com.evil.com/acme/demo") is None     # subdomain trick
    assert f("/srv/x/github.com/acme/demo") is None               # github.com in a path, not the host


def test_issue_reference_parser_ignores_other_repositories():
    body = "Fixes #1 and resolves acme/demo#2; mentions acme/other#3 and #4."
    assert git_info._same_repo_issue_refs(body, "acme", "demo") == {1, 2, 4}
    assert git_info._same_repo_closing_refs(body, "acme", "demo") == {1, 2}


def test_issue_relationship_refresh_removes_stale_and_legacy_edges(graph):
    writer = GraphWriter(graph)
    repo_id = "github://acme/demo"
    nodes = [
        GraphNode("GitIssue", {"id": f"{repo_id}/issue/4"}, {"id": f"{repo_id}/issue/4"}),
        GraphNode("GitPullRequest", {"id": f"{repo_id}/pull/5"}, {"id": f"{repo_id}/pull/5"}),
        GraphNode("GitRelease", {"id": f"{repo_id}/release/6"}, {"id": f"{repo_id}/release/6"}),
        GraphNode("GitUser", {"id": f"{repo_id}/user/a"}, {"id": f"{repo_id}/user/a"}),
    ]
    writer.merge_nodes(nodes)
    writer.merge_edges([
        GraphEdge("CREATED_BY", "GitIssue", {"id": f"{repo_id}/issue/4"}, "GitUser", {"id": f"{repo_id}/user/a"}),
        GraphEdge("OPENED_BY", "GitPullRequest", {"id": f"{repo_id}/pull/5"}, "GitUser", {"id": f"{repo_id}/user/a"}),
        GraphEdge("REFERENCES_ISSUE", "GitPullRequest", {"id": f"{repo_id}/pull/5"}, "GitIssue", {"id": f"{repo_id}/issue/4"}),
        GraphEdge("CLOSES_ISSUE", "GitPullRequest", {"id": f"{repo_id}/pull/5"}, "GitIssue", {"id": f"{repo_id}/issue/4"}),
        GraphEdge("CREATED_BY", "GitRelease", {"id": f"{repo_id}/release/6"}, "GitUser", {"id": f"{repo_id}/user/a"}),
    ])

    git_info._replace_github_issue_edges(writer, repo_id)
    remaining = graph.ro_query("MATCH ()-[e]->() RETURN type(e)").result_set
    assert remaining == [["CREATED_BY"]]


def test_github_checks_capture_suites_runs_and_publishing_apps(monkeypatch):
    monkeypatch.setattr(git_info, "_github_token", lambda: None)   # no token -> the REST fallback path
    sha = "a" * 40
    missing_sha = "f" * 40

    def api_pages(path):
        if missing_sha in path:
            raise git_info.urllib.error.HTTPError(path, 422, "unprocessable", {}, None)
        return [{"check_runs": [
        {"id": 11, "name": "lint", "status": "completed", "conclusion": "success",
         "head_sha": sha, "html_url": "https://github.com/acme/demo/runs/11",
         "started_at": "2025-01-01T00:00:00Z", "completed_at": "2025-01-01T00:01:00Z",
         "check_suite": {"id": 7, "status": "completed", "conclusion": "success",
                         "before": "b" * 40, "after": sha},
         "app": {"id": 3, "slug": "lint-app", "name": "Lint App",
                 "html_url": "https://github.com/apps/lint-app"}},
        ]}]

    monkeypatch.setattr(git_info, "_api_pages", api_pages)
    nodes, edges = git_info._github_checks("acme", "demo", "github://acme/demo", [sha, missing_sha])
    by_label = {node.label: node for node in nodes}
    assert set(by_label) == {"GitCheckSuite", "GitCheckRun", "GitHubApp"}
    assert by_label["GitCheckSuite"].properties["conclusion"] == "success"
    assert by_label["GitCheckRun"].properties["name"] == "lint"
    assert by_label["GitHubApp"].properties["slug"] == "lint-app"
    assert {edge.rel_type for edge in edges} == {"HAS_CHECK_SUITE", "HAS_CHECK_RUN", "PROVIDED_BY"}


def test_github_checks_via_graphql_batches_commits(monkeypatch):
    # With a token, checks come from batched GraphQL (databaseId -> the same ids the REST path uses).
    monkeypatch.setattr(git_info, "_github_token", lambda: "tok")
    sha = "a" * 40

    def graphql(query, variables):
        assert sha in query and variables == {"o": "acme", "n": "demo"}  # the oid is batched into the query
        return {"repository": {"c0": {
            "oid": sha,
            "checkSuites": {"nodes": [{
                "databaseId": 7, "status": "COMPLETED", "conclusion": "SUCCESS",
                "createdAt": "2025-01-01T00:00:00Z", "updatedAt": "2025-01-01T00:01:00Z",
                "app": {"databaseId": 3, "slug": "lint-app", "name": "Lint App",
                        "url": "https://github.com/apps/lint-app"},
                "checkRuns": {"nodes": [{
                    "databaseId": 11, "name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS",
                    "detailsUrl": "https://github.com/acme/demo/runs/11",
                    "startedAt": "2025-01-01T00:00:00Z", "completedAt": "2025-01-01T00:01:00Z"}]},
            }]},
        }}}

    monkeypatch.setattr(git_info, "_graphql", graphql)
    nodes, edges = git_info._github_checks("acme", "demo", "github://acme/demo", [sha])
    by_label = {node.label: node for node in nodes}
    assert set(by_label) == {"GitCheckSuite", "GitCheckRun", "GitHubApp"}
    assert by_label["GitCheckSuite"].properties["suite_id"] == "7"
    assert by_label["GitCheckRun"].properties["name"] == "lint"
    assert by_label["GitHubApp"].properties["slug"] == "lint-app"
    assert by_label["GitCheckSuite"].key["id"] == "github://acme/demo/check-suite/7"
    assert {edge.rel_type for edge in edges} == {"HAS_CHECK_SUITE", "HAS_CHECK_RUN", "PROVIDED_BY"}


def test_commit_user_prefers_github_login_over_git_identity():
    logins = {"sha1": {"author": "octocat", "committer": "web-flow"}}
    assert git_info._commit_user(logins, "sha1", "author", "a@x.io", "Alice") == ("github:octocat", "octocat", "github")
    assert git_info._commit_user(logins, "sha1", "committer", "c@x.io", "C") == ("github:web-flow", "web-flow", "github")
    assert git_info._commit_user(logins, "unknown", "author", "b@x.io", "Bob") == ("b@x.io", "Bob", "git")
    assert git_info._commit_user({}, "s", "author", None, "Only Name") == ("Only Name", "Only Name", "git")


def test_commit_and_pr_by_same_github_login_share_one_gituser(graph, tmp_path, monkeypatch):
    # No duplicate identity per person: a commit authored by GitHub login "ada" must land on the SAME
    # GitUser node that opens a PR as "ada" -- not a second node keyed by the git email.
    repo = _make_repo(tmp_path / "checkout")
    writer = GraphWriter(graph)
    head = repo.head.commit.hexsha
    monkeypatch.setattr(git_info, "_verify_public_github", lambda *_: None)

    def meta(owner, name, repo_id, default_branch=None):
        # mirror _github_metadata.add_github_user: the identity is URL-quoted in the node id
        uid = f"{repo_id}/user/{git_info.urllib.parse.quote('github:ada', safe='')}"
        pid = f"{repo_id}/pull/1"
        nodes = [GraphNode("GitPullRequest", {"id": pid}, {"id": pid, "number": 1}),
                 GraphNode("GitUser", {"id": uid},
                           {"id": uid, "identity": "github:ada", "name": "ada", "source": "github"})]
        return nodes, [GraphEdge("OPENED_BY", "GitPullRequest", {"id": pid}, "GitUser", {"id": uid})]

    monkeypatch.setattr(git_info, "_github_metadata", meta)
    monkeypatch.setattr(git_info, "_github_checks", lambda *_: ([], []))
    monkeypatch.setattr(git_info, "_github_commit_logins",
                        lambda *a, **k: {head: {"author": "ada", "committer": "ada"}})

    git_info.sync_git_info(writer, repo.working_tree_dir, force_remote=True)

    assert _count(graph, "GitUser") == 1  # commit author + PR author collapsed to one node
    rels = graph.ro_query(
        "MATCH ()-[e]->(:GitUser {identity:'github:ada'}) RETURN DISTINCT type(e)"
    ).result_set
    assert {r[0] for r in rels} == {"AUTHORED", "COMMITTED", "OPENED_BY"}  # all layers point at it
    assert graph.ro_query(
        "MATCH (u:GitUser {identity:'ada@example.org'}) RETURN count(u)"
    ).result_set[0][0] == 0  # no stray git-email duplicate


def test_public_gate_rejects_private_and_unverifiable_repos(monkeypatch):
    monkeypatch.setattr(git_info, "_api_pages", lambda _: [{"private": True, "visibility": "private"}])
    with pytest.raises(PermissionError):
        git_info._verify_public_github("acme", "private")

    monkeypatch.setattr(git_info, "_api_pages", lambda _: [{"private": False, "visibility": "public"}])
    git_info._verify_public_github("acme", "public")

    monkeypatch.setattr(git_info, "_api_pages", lambda _: [{"name": "visibility missing"}])
    with pytest.raises(PermissionError):
        git_info._verify_public_github("acme", "unknown")


def test_github_pagination_is_anonymous_and_rejects_cross_origin(monkeypatch):
    monkeypatch.setattr(git_info, "_github_token", lambda: None)   # anonymous: no Authorization header
    requests = []

    class Response:
        def __init__(self, payload, link=""):
            self.payload = payload
            self.headers = {"Link": link}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            import json
            return json.dumps(self.payload).encode()

    responses = iter((
        Response([{"number": 1}], '<https://api.github.com/repos/acme/demo/issues?page=2>; rel="next"'),
        Response([{"number": 2}]),
    ))

    def open_request(request, timeout):
        requests.append(request)
        return next(responses)

    monkeypatch.setattr(git_info.urllib.request, "urlopen", open_request)
    assert git_info._api_pages("repos/acme/demo/issues") == [{"number": 1}, {"number": 2}]
    assert len(requests) == 2
    assert all("Authorization" not in request.headers for request in requests)

    monkeypatch.setattr(git_info.urllib.request, "urlopen", lambda *_args, **_kwargs: Response(
        [], '<https://example.com/steal>; rel="next"'
    ))
    with pytest.raises(ValueError, match="unexpected URL"):
        git_info._api_pages("repos/acme/demo/issues")


def test_private_or_unverified_remote_removes_prior_checkout_data(graph, tmp_path, monkeypatch):
    repo = _make_repo(tmp_path / "checkout")
    writer = GraphWriter(graph)
    monkeypatch.setattr(git_info, "_verify_public_github", lambda *_: None)
    monkeypatch.setattr(git_info, "_github_metadata", lambda *_: ([], []))
    monkeypatch.setattr(git_info, "_github_checks", lambda *_: ([], []))
    monkeypatch.setattr(git_info, "_github_commit_logins", lambda *a, **k: {})
    git_info.sync_git_info(writer, repo.working_tree_dir)
    assert _count(graph, "GitRepository") == 1

    def reject_private(*_):
        raise PermissionError("not public")

    monkeypatch.setattr(git_info, "_verify_public_github", reject_private)
    result = git_info.sync_git_info(writer, repo.working_tree_dir)
    assert result["skipped"] == "public visibility check failed"
    assert _count(graph, "GitRepository") == 0
    assert _count(graph, "GitCommit") == 0


def test_temporary_visibility_api_failure_preserves_existing_data(graph, tmp_path, monkeypatch):
    repo = _make_repo(tmp_path / "checkout")
    writer = GraphWriter(graph)
    monkeypatch.setattr(git_info, "_verify_public_github", lambda *_: None)
    monkeypatch.setattr(git_info, "_github_metadata", lambda *_: ([], []))
    monkeypatch.setattr(git_info, "_github_checks", lambda *_: ([], []))
    monkeypatch.setattr(git_info, "_github_commit_logins", lambda *a, **k: {})
    git_info.sync_git_info(writer, repo.working_tree_dir)
    assert _count(graph, "GitRepository") == 1

    def api_unavailable(*_):
        raise git_info.urllib.error.URLError("temporary network failure")

    monkeypatch.setattr(git_info, "_verify_public_github", api_unavailable)
    result = git_info.sync_git_info(writer, repo.working_tree_dir)
    assert result["skipped"] == "public visibility check failed"
    assert _count(graph, "GitRepository") == 1
    assert _count(graph, "GitCommit") == 1


def test_ingest_toggle_on_remote_change_and_off_reconciles_graph(
    graph, tmp_path, monkeypatch
):
    root = tmp_path / "checkout"
    repo = _make_repo(root)
    writer = GraphWriter(graph)
    monkeypatch.setattr(git_info, "_verify_public_github", lambda *_: None)
    monkeypatch.setattr(git_info, "_github_metadata", lambda *_: ([], []))
    monkeypatch.setattr(git_info, "_github_checks", lambda *_: ([], []))
    monkeypatch.setattr(git_info, "_github_commit_logins", lambda *a, **k: {})
    writer.merge_node(GraphNode("KeepNode", {"id": "keep"}, {"id": "keep"}))

    set_git_info_enabled(True)
    ingest.ingest_directory(writer, str(root), project_name="Checkout", deep="off", auto_install=False)
    assert _count(graph, "GitRepository") == 1
    assert _count(graph, "GitCommit") == 1
    assert _count(graph, "GitCheckout") == 1

    repo.remotes.origin.set_url("https://github.com/acme/renamed.git")
    ingest.ingest_directory(writer, str(root), project_name="Checkout", deep="off", auto_install=False)
    assert graph.ro_query("MATCH (r:GitRepository) RETURN r.id").result_set == [
        ["github://acme/renamed"]
    ]

    # If the checkout loses its .git directory, the now-plain source must unlink stale
    # provenance for that checkout while leaving the source/code graph intact.
    shutil.rmtree(root / ".git")
    ingest.ingest_directory(writer, str(root), project_name="Checkout", deep="off", auto_install=False)
    assert _count(graph, "GitRepository") == 0

    set_git_info_enabled(False)
    ingest.ingest_directory(writer, str(root), project_name="Checkout", deep="off", auto_install=False)
    assert _count(graph, "GitRepository") == 0
    assert _count(graph, "GitCommit") == 0
    assert _count(graph, "KeepNode") == 1


def test_enable_command_queues_background_sync_for_existing_checkout(graph, tmp_path, monkeypatch):
    from click.testing import CliRunner
    from auryn import cli as cli_module
    from auryn.core.app_config import load_config
    from auryn.core.graph_client import FalkorDBConfig

    root = tmp_path / "checkout"
    _make_repo(root)
    calls = []
    monkeypatch.setattr(cli_module, "_resolve_connection", lambda *_: FalkorDBConfig())
    monkeypatch.setattr(cli_module, "connect", lambda _config: graph)
    monkeypatch.setattr(git_info, "sync_git_info", lambda writer, path, **kw: calls.append(
        (path, kw.get("force_remote"))) or {"repository": "https://github.com/acme/one"})

    result = CliRunner().invoke(cli_module.cli, ["git-info", "enable", str(root)])
    assert result.exit_code == 0, result.output
    assert load_config().git_info_enabled is True
    assert calls == [(str(root), True)]


def test_watch_command_force_refreshes_each_tick_until_interrupted(graph, tmp_path, monkeypatch):
    import signal as _signal
    import time as _time
    from click.testing import CliRunner
    from auryn import cli as cli_module
    from auryn.core.graph_client import FalkorDBConfig

    root = tmp_path / "checkout"
    _make_repo(root)
    set_git_info_enabled(True)  # git_info_sync skips when the feature is off
    calls = []
    monkeypatch.setattr(cli_module, "_resolve_connection", lambda *_: FalkorDBConfig())
    monkeypatch.setattr(cli_module, "connect", lambda _config: graph)
    monkeypatch.setattr(git_info, "sync_git_info", lambda writer, path, **kw: calls.append(
        (path, kw.get("force_remote"))) or {"repository": "https://github.com/acme/one"})
    # Don't clobber the test runner's real signal handlers.
    monkeypatch.setattr(_signal, "signal", lambda *a, **k: None)

    class _Break(Exception):
        pass

    # The watcher sleeps between ticks; raising from the first sleep ends it after exactly one refresh.
    def _stop_after_first_tick(_seconds):
        raise _Break()

    monkeypatch.setattr(_time, "sleep", _stop_after_first_tick)

    result = CliRunner().invoke(cli_module.cli, ["git-info", "watch", str(root), "--interval", "900"])
    assert isinstance(result.exception, _Break)  # loop was still running (interrupt-driven), not a crash
    assert calls == [(str(root), True)]  # one tick, and it forced a remote refresh


def test_watch_command_without_path_refreshes_every_registered_repo(graph, tmp_path, monkeypatch):
    import signal as _signal
    import time as _time
    from click.testing import CliRunner
    from auryn import cli as cli_module
    from auryn.core.app_config import register_repo, RepoRegistration
    from auryn.core.graph_client import FalkorDBConfig

    repo_a = tmp_path / "alpha"
    repo_b = tmp_path / "beta"
    _make_repo(repo_a)
    _make_repo(repo_b)
    # Two repos in two different graphs — the whole point: one daemon, multiple projects.
    register_repo(RepoRegistration(local_dir=str(repo_a.resolve()), graph_name="alpha_graph"))
    register_repo(RepoRegistration(local_dir=str(repo_b.resolve()), graph_name="beta_graph"))
    set_git_info_enabled(True)  # git_info_sync skips when the feature is off

    calls = []
    monkeypatch.setattr(cli_module, "_resolve_connection", lambda *_: FalkorDBConfig())
    monkeypatch.setattr(cli_module, "connect", lambda _config: graph)
    monkeypatch.setattr(git_info, "sync_git_info",
                        lambda writer, path, **kw: calls.append(path) or {"repository": path})
    monkeypatch.setattr(_signal, "signal", lambda *a, **k: None)

    class _Break(Exception):
        pass

    def _stop_after_first_tick(_seconds):
        raise _Break()

    monkeypatch.setattr(_time, "sleep", _stop_after_first_tick)

    # No PATH and no --graph -> multi-project mode: enumerate the registry, refresh each.
    result = CliRunner().invoke(cli_module.cli, ["git-info", "watch", "--interval", "900"])
    assert isinstance(result.exception, _Break)
    assert set(calls) == {str(repo_a.resolve()), str(repo_b.resolve())}  # both repos in one tick


def test_urlopen_retrying_recovers_from_transport_drop(monkeypatch):
    import http.client

    attempts = {"n": 0}

    class _Resp:
        headers = {"Link": ""}

        def read(self):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(request, timeout):
        attempts["n"] += 1
        if attempts["n"] == 1:  # GitHub drops the connection mid-fetch, then recovers
            raise http.client.RemoteDisconnected("Remote end closed connection without response")
        return _Resp()

    monkeypatch.setattr(git_info.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(git_info.time, "sleep", lambda *_: None)  # don't actually back off in the test

    body, headers = git_info._urlopen_retrying(
        git_info.urllib.request.Request("https://api.github.com/x"), timeout=5)
    assert attempts["n"] == 2  # retried once after the drop
    assert body == b'{"ok": true}'


def test_urlopen_retrying_recovers_from_incomplete_read(monkeypatch):
    # A truncated body raises http.client.IncompleteRead, which is neither URLError nor ConnectionError
    # -- it must still be treated as a transient transport drop and retried.
    import http.client

    attempts = {"n": 0}

    class _Resp:
        headers = {"Link": ""}

        def read(self):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(request, timeout):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise http.client.IncompleteRead(partial=b"trunc")
        return _Resp()

    monkeypatch.setattr(git_info.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(git_info.time, "sleep", lambda *_: None)

    body, _ = git_info._urlopen_retrying(
        git_info.urllib.request.Request("https://api.github.com/x"), timeout=5)
    assert attempts["n"] == 2 and body == b'{"ok": true}'


def test_urlopen_retrying_normalizes_exhausted_transport_drop_to_urlerror(monkeypatch):
    import http.client

    def fake_urlopen(request, timeout):
        raise http.client.RemoteDisconnected("down")

    monkeypatch.setattr(git_info.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(git_info.time, "sleep", lambda *_: None)

    # An exhausted BARE transport drop is normalized to URLError so every caller (which all catch
    # URLError) degrades that one fetch instead of the raw exception aborting the whole sync.
    with pytest.raises(git_info.urllib.error.URLError) as caught:
        git_info._urlopen_retrying(
            git_info.urllib.request.Request("https://api.github.com/x"), timeout=5, retries=2)
    assert isinstance(caught.value.__cause__, http.client.RemoteDisconnected)

