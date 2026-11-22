"""The CI producer against the real `graph` fixture: a workflow -> Resource + BUILDS_IMAGE, path
anchoring on `.github/workflows/`, and resync. Needs FalkorDB (the conftest fixture)."""

from __future__ import annotations

from auryn.core.graph_client import GraphWriter
from auryn.core.local_source import LocalDirectorySource
from auryn.ingestors.ci.git_sync import sync_at

_WF = (
    "name: build\non: [push]\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n"
    "      - uses: docker/build-push-action@v5\n        with:\n          push: true\n"
    "          tags: shop/api:1.0\n"
)


def _ingest(graph, tmp_path, files, project="shop"):
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    src = LocalDirectorySource(str(tmp_path))
    sync_at(GraphWriter(graph), src, src.tree_sha(), project_name=project, repo_uri="test://ci", ref=None)
    return lambda c: graph.query(c).result_set


def test_workflow_resource_and_file(graph, tmp_path):
    q = _ingest(graph, tmp_path, {".github/workflows/build.yml": _WF})
    assert q("MATCH (r:Resource {kind:'ci-workflow', name:'build'}) RETURN count(r)") == [[1]]
    assert q("MATCH (:File {name:'build.yml'})-[:HAS_RESOURCE]->(:Resource) RETURN count(*)") == [[1]]


def test_builds_image_joins_hub(graph, tmp_path):
    q = _ingest(graph, tmp_path, {".github/workflows/build.yml": _WF})
    assert q("MATCH (:Resource {name:'build'})-[:BUILDS_IMAGE]->(i:Image) RETURN i.ref") == [["docker.io/shop/api:1.0"]]


def test_only_github_workflows_are_ingested(graph, tmp_path):
    # a YAML with a `jobs:` map OUTSIDE `.github/workflows/` is not a workflow here (path anchor); a
    # file inside WITHOUT a `jobs:` map is dropped
    q = _ingest(graph, tmp_path, {
        ".github/workflows/build.yml": _WF,
        "jobs.yml": "jobs:\n  x:\n    steps:\n      - run: docker build -t other/x:9 .\n",  # not under the dir
        ".github/workflows/notawf.yml": "name: x\non: [push]\n",                             # no jobs
    })
    assert q("MATCH (r:Resource) RETURN count(r)") == [[1]]
    assert q("MATCH (:Resource)-[:BUILDS_IMAGE]->(i:Image) RETURN i.ref") == [["docker.io/shop/api:1.0"]]


def test_resync_removes_deleted_workflow(graph, tmp_path):
    q = _ingest(graph, tmp_path, {".github/workflows/build.yml": _WF,
                                  ".github/workflows/other.yml": "name: o\non: [push]\njobs:\n  b:\n    steps:\n      - run: docker push a/b:1\n"})
    assert q("MATCH (r:Resource) RETURN count(r)") == [[2]]
    (tmp_path / ".github/workflows/other.yml").unlink()
    q = _ingest(graph, tmp_path, {".github/workflows/build.yml": _WF})
    assert sorted(r[0] for r in q("MATCH (r:Resource) RETURN r.name")) == ["build"]
    # the removed workflow's BUILDS_IMAGE edge is gone (the shared Image node itself is GC'd by the stitch)
    assert q("MATCH (:Resource)-[:BUILDS_IMAGE]->(:Image {ref:'docker.io/a/b:1'}) RETURN count(*)") == [[0]]


def test_repo_with_no_workflows_is_not_ingested(graph, tmp_path):
    q = _ingest(graph, tmp_path, {"config.yml": "foo: bar\n", "k8s/deploy.yaml": "apiVersion: v1\nkind: Pod\n"})
    assert q("MATCH (p:Project {name:'shop'}) RETURN count(p)") == [[0]]
    assert q("MATCH (r:Resource) RETURN count(r)") == [[0]]


def test_nested_workflow_subdir_is_ignored(graph, tmp_path):
    # GitHub reads only DIRECT children of .github/workflows/; a nested file is not a workflow
    q = _ingest(graph, tmp_path, {
        ".github/workflows/build.yml": _WF,
        ".github/workflows/archive/old.yml": _WF.replace("shop/api:1.0", "old/x:1"),
    })
    assert q("MATCH (r:Resource) RETURN count(r)") == [[1]]
    assert q("MATCH (i:Image) WHERE i.ref CONTAINS 'old/x' RETURN count(i)") == [[0]]
