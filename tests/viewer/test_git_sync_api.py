"""Integration test for the viewer's one write-capable endpoint (POST .../git-sync)
-- every other endpoint in tests/viewer/test_api.py stays exactly as read-only as
it was before this feature."""

from __future__ import annotations

import git
import pytest

from auryn.core.graph_client import GraphWriter
from auryn.core.graph_model import GraphNode
from auryn.ingestors.csharp.git_sync import GitCSharpProjectConfig, sync
from auryn.viewer.app import create_app

_WIDGET_CS = """
namespace Acme
{
    public class Widget
    {
        public int Id { get; set; }
    }
}
"""

_CONTROLLER_CS = """
using Microsoft.AspNetCore.Mvc;

namespace Acme
{
    [ApiController]
    [Route("api/[controller]")]
    public class WidgetsController : ControllerBase
    {
        [HttpGet]
        public Widget GetOne()
        {
            return new Widget();
        }
    }
}
"""


@pytest.fixture
def origin_repo(tmp_path):
    origin_path = tmp_path / "origin"
    repo = git.Repo.init(str(origin_path), initial_branch="main")
    with repo.config_writer() as cw:
        cw.set_value("user", "email", "test@example.com")
        cw.set_value("user", "name", "Test")

    (origin_path / "src").mkdir()
    (origin_path / "src" / "Widget.cs").write_text(_WIDGET_CS)
    repo.index.add(["src/Widget.cs"])
    first_commit = repo.index.commit("plain Widget class").hexsha

    (origin_path / "src" / "WidgetsController.cs").write_text(_CONTROLLER_CS)
    repo.index.add(["src/WidgetsController.cs"])
    second_commit = repo.index.commit("add endpoint returning Widget").hexsha

    return origin_path, first_commit, second_commit


def _seeded_client(origin_repo, graph, falkordb_connection, tmp_path):
    origin_path, first_commit, second_commit = origin_repo
    config = GitCSharpProjectConfig(repo_uri=str(origin_path), project_name="Widgets", workers=1)
    sync(GraphWriter(graph), config, ref=first_commit, cache_root=tmp_path / "cache")
    app = create_app(falkordb_connection)
    return app.test_client(), graph.name, second_commit


def test_git_projects_endpoint_lists_only_git_tracked_projects(origin_repo, graph, falkordb_connection, tmp_path):
    client, graph_name, _ = _seeded_client(origin_repo, graph, falkordb_connection, tmp_path)
    GraphWriter(graph).merge_node(GraphNode("Project", {"id": "csharp://NotGitTracked"}, {"name": "NotGitTracked"}))

    resp = client.get(f"/api/graphs/{graph_name}/git-projects")

    assert resp.status_code == 200
    names = [p["name"] for p in resp.get_json()]
    assert names == ["Widgets"]


def test_git_projects_endpoint_unknown_graph_returns_404(falkordb_connection):
    app = create_app(falkordb_connection)
    client = app.test_client()

    resp = client.get("/api/graphs/totally_nonexistent_graph_xyz/git-projects")

    assert resp.status_code == 404


def test_git_refs_endpoint_lists_branches_and_tags(origin_repo, graph, falkordb_connection, tmp_path):
    origin_path, _, _ = origin_repo
    origin = git.Repo(str(origin_path))
    origin.create_tag("v1.0")
    origin.git.checkout("-b", "feature")
    origin.git.checkout("main")

    client, graph_name, _ = _seeded_client(origin_repo, graph, falkordb_connection, tmp_path)

    resp = client.get(f"/api/graphs/{graph_name}/git-refs", query_string={"project_id": "csharp://Widgets"})

    assert resp.status_code == 200
    data = resp.get_json()
    assert set(data["branches"]) == {"main", "feature"}
    assert data["tags"] == ["v1.0"]
    assert data["default_branch"] == "main"


def test_git_refs_endpoint_requires_project_id(origin_repo, graph, falkordb_connection, tmp_path):
    client, graph_name, _ = _seeded_client(origin_repo, graph, falkordb_connection, tmp_path)

    resp = client.get(f"/api/graphs/{graph_name}/git-refs")

    assert resp.status_code == 400


def test_git_refs_endpoint_rejects_a_project_without_repo_uri(graph, falkordb_connection):
    GraphWriter(graph).merge_node(GraphNode("Project", {"id": "csharp://NotGitTracked"}, {"name": "NotGitTracked"}))
    app = create_app(falkordb_connection)
    client = app.test_client()

    resp = client.get(f"/api/graphs/{graph.name}/git-refs", query_string={"project_id": "csharp://NotGitTracked"})

    assert resp.status_code == 400


def _seed_unreachable_project(graph):
    GraphWriter(graph).merge_node(
        GraphNode(
            "Project",
            {"id": "csharp://Unreachable"},
            {"name": "Unreachable", "repo_uri": "/nonexistent/path/to/a/repo"},
        )
    )


def test_git_refs_endpoint_returns_502_not_500_for_an_unreachable_repo(graph, falkordb_connection):
    _seed_unreachable_project(graph)
    app = create_app(falkordb_connection)
    client = app.test_client()

    resp = client.get(f"/api/graphs/{graph.name}/git-refs", query_string={"project_id": "csharp://Unreachable"})

    assert resp.status_code == 502
    assert "Unreachable" not in resp.get_json()["error"]  # sanity: it's the repo_uri, not the project name, in the message


def test_git_sync_endpoint_returns_502_not_500_for_an_unreachable_repo(graph, falkordb_connection):
    _seed_unreachable_project(graph)
    app = create_app(falkordb_connection)
    client = app.test_client()

    resp = client.post(f"/api/graphs/{graph.name}/git-sync", json={"project_id": "csharp://Unreachable"})

    assert resp.status_code == 502


def test_git_sync_endpoint_returns_400_not_500_for_an_unresolvable_ref(origin_repo, graph, falkordb_connection, tmp_path):
    client, graph_name, _ = _seeded_client(origin_repo, graph, falkordb_connection, tmp_path)

    resp = client.post(
        f"/api/graphs/{graph_name}/git-sync",
        json={"project_id": "csharp://Widgets", "ref": "not-a-real-ref"},
    )

    assert resp.status_code == 400
    assert "not-a-real-ref" in resp.get_json()["error"]


def test_git_sync_endpoint_advances_the_graph_to_the_given_ref(origin_repo, graph, falkordb_connection, tmp_path):
    client, graph_name, second_commit = _seeded_client(origin_repo, graph, falkordb_connection, tmp_path)

    resp = client.post(f"/api/graphs/{graph_name}/git-sync", json={"project_id": "csharp://Widgets", "ref": second_commit})

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["commit_sha"] == second_commit
    assert data["nodes_added"] > 0

    node_resp = client.get(f"/api/graphs/{graph_name}/search", query_string={"label": "DTO"})
    assert [n["name"] for n in node_resp.get_json()] == ["Widget"]


def test_git_sync_endpoint_defaults_ref_to_the_default_branch(origin_repo, graph, falkordb_connection, tmp_path):
    client, graph_name, second_commit = _seeded_client(origin_repo, graph, falkordb_connection, tmp_path)

    resp = client.post(f"/api/graphs/{graph_name}/git-sync", json={"project_id": "csharp://Widgets"})

    assert resp.status_code == 200
    assert resp.get_json()["commit_sha"] == second_commit


def test_git_sync_endpoint_requires_project_id(origin_repo, graph, falkordb_connection, tmp_path):
    client, graph_name, _ = _seeded_client(origin_repo, graph, falkordb_connection, tmp_path)

    resp = client.post(f"/api/graphs/{graph_name}/git-sync", json={})

    assert resp.status_code == 400


def test_git_sync_endpoint_unknown_project_returns_404(origin_repo, graph, falkordb_connection, tmp_path):
    client, graph_name, _ = _seeded_client(origin_repo, graph, falkordb_connection, tmp_path)

    resp = client.post(f"/api/graphs/{graph_name}/git-sync", json={"project_id": "csharp://DoesNotExist"})

    assert resp.status_code == 404


def test_git_sync_endpoint_rejects_a_non_project_node(origin_repo, graph, falkordb_connection, tmp_path):
    client, graph_name, _ = _seeded_client(origin_repo, graph, falkordb_connection, tmp_path)

    resp = client.post(
        f"/api/graphs/{graph_name}/git-sync",
        json={"project_id": "csharp://Widgets/ns/acme.class.widget"},
    )

    assert resp.status_code == 404


def test_git_sync_endpoint_rejects_a_project_without_repo_uri(graph, falkordb_connection):
    GraphWriter(graph).merge_node(GraphNode("Project", {"id": "csharp://NotGitTracked"}, {"name": "NotGitTracked"}))
    app = create_app(falkordb_connection)
    client = app.test_client()

    resp = client.post(f"/api/graphs/{graph.name}/git-sync", json={"project_id": "csharp://NotGitTracked"})

    assert resp.status_code == 400


def test_git_sync_endpoint_unknown_graph_returns_404(falkordb_connection):
    app = create_app(falkordb_connection)
    client = app.test_client()

    resp = client.post("/api/graphs/totally_nonexistent_graph_xyz/git-sync", json={"project_id": "csharp://Widgets"})

    assert resp.status_code == 404
