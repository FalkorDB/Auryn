"""The Helm producer against the real `graph` fixture: a chart -> Resource (chart) + Service (workload)
+ Image, with DEPENDS_ON/DEPLOYS/RUNS_IMAGE edges, and resync. Needs FalkorDB (the conftest fixture)."""

from __future__ import annotations

from auryn.core.graph_client import GraphWriter
from auryn.core.local_source import LocalDirectorySource
from auryn.ingestors.helm.git_sync import sync_at

_CHART = "apiVersion: v2\nname: shop-api\nversion: 1.0.0\ndependencies:\n  - name: redis\n"
_VALUES = "image:\n  repository: shop/api\n  tag: \"1.0\"\n"
_DEPLOYMENT = (
    "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: shop-api\nspec:\n  template:\n    spec:\n"
    '      containers:\n        - name: api\n          image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"\n'
)

_CHART_FILES = {
    "charts/shop/Chart.yaml": _CHART,
    "charts/shop/values.yaml": _VALUES,
    "charts/shop/templates/deployment.yaml": _DEPLOYMENT,
}


def _ingest(graph, tmp_path, files, project="shop"):
    # clear any files from a prior _ingest in the same tmp_path so resync sees a true deletion
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    src = LocalDirectorySource(str(tmp_path))
    sync_at(GraphWriter(graph), src, src.tree_sha(), project_name=project, repo_uri="test://helm", ref=None)
    return lambda c: graph.query(c).result_set


def test_chart_resource_and_file(graph, tmp_path):
    q = _ingest(graph, tmp_path, _CHART_FILES)
    assert q("MATCH (r:Resource {kind:'chart', name:'shop-api'}) RETURN count(r)") == [[1]]
    assert q("MATCH (:File {name:'Chart.yaml'})-[:HAS_RESOURCE]->(:Resource {name:'shop-api'}) RETURN count(*)") == [[1]]


def test_workload_deployed_and_runs_hub_image(graph, tmp_path):
    q = _ingest(graph, tmp_path, _CHART_FILES)
    assert q("MATCH (:Resource {name:'shop-api'})-[:DEPLOYS]->(s:Service) RETURN s.name, s.kind") == [["shop-api", "Deployment"]]
    assert q("MATCH (:Service {name:'shop-api'})-[:RUNS_IMAGE]->(i:Image) RETURN i.ref") == [["docker.io/shop/api:1.0"]]


def test_external_dependency_is_a_placeholder(graph, tmp_path):
    q = _ingest(graph, tmp_path, _CHART_FILES)
    assert q("MATCH (:Resource {name:'shop-api'})-[:DEPENDS_ON]->(d:Resource) RETURN d.name, d.resolved") == [["redis", False]]


def test_in_repo_dependency_resolves_to_the_chart(graph, tmp_path):
    files = dict(_CHART_FILES)
    files["charts/shop/Chart.yaml"] = "apiVersion: v2\nname: shop-api\nversion: 1.0.0\ndependencies:\n  - name: cache\n"
    files["charts/cache/Chart.yaml"] = "apiVersion: v2\nname: cache\nversion: 2.0.0\n"
    q = _ingest(graph, tmp_path, files)
    # the dep resolves to the REAL in-repo chart (resolved:true), not a placeholder
    assert q("MATCH (:Resource {name:'shop-api'})-[:DEPENDS_ON]->(d:Resource {name:'cache'}) RETURN d.resolved") == [[True]]
    assert q("MATCH (d:Resource {name:'cache', resolved:false}) RETURN count(d)") == [[0]]


def test_resync_removes_deleted_chart(graph, tmp_path):
    q = _ingest(graph, tmp_path, _CHART_FILES)
    assert q("MATCH (s:Service) RETURN count(s)") == [[1]]
    # remove the deployment template -> the workload Service disappears; keep only the chart
    import shutil
    shutil.rmtree(tmp_path / "charts" / "shop" / "templates")
    q = _ingest(graph, tmp_path, {"charts/shop/Chart.yaml": "apiVersion: v2\nname: shop-api\nversion: 2.0.0\n"})
    assert q("MATCH (s:Service) RETURN count(s)") == [[0]]
    assert q("MATCH (:Resource)-[:DEPENDS_ON]->() RETURN count(*)") == [[0]]  # the redis dep is gone too


# ── id-collision hardening ──────────────────────────────────────────────────────
_TEMPLATED_WORKLOAD = (
    "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n"
    '  name: {{ include "x.fullname" . }}\nspec:\n  template:\n    spec:\n      containers:\n        - image: a:1\n'
)


def test_nested_same_basename_templates_stay_distinct(graph, tmp_path):
    # two templates with the SAME basename and a templated (fallback-to-stem) name -- the chart-relative
    # template PATH (not just the basename) keeps their Service nodes distinct
    files = {
        "charts/shop/Chart.yaml": "apiVersion: v2\nname: shop\nversion: 1.0.0\n",
        "charts/shop/templates/a/deploy.yaml": _TEMPLATED_WORKLOAD,
        "charts/shop/templates/b/deploy.yaml": _TEMPLATED_WORKLOAD,
    }
    q = _ingest(graph, tmp_path, files)
    assert q("MATCH (s:Service) RETURN count(s)") == [[2]]


def test_same_name_different_kind_stay_distinct(graph, tmp_path):
    # same name + same template file, different kind -> kind is part of the id, so two Services
    multi = (
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: worker\nspec:\n  template:\n    spec:\n"
        "      containers:\n        - image: a:1\n---\n"
        "apiVersion: batch/v1\nkind: CronJob\nmetadata:\n  name: worker\n"
    )
    files = {
        "charts/shop/Chart.yaml": "apiVersion: v2\nname: shop\nversion: 1.0.0\n",
        "charts/shop/templates/w.yaml": multi,
    }
    q = _ingest(graph, tmp_path, files)
    assert q("MATCH (s:Service {name:'worker'}) RETURN count(s)") == [[2]]


def test_root_chart_and_sibling_dir_do_not_merge(graph, tmp_path):
    # a ROOT chart whose Chart.yaml name equals a sibling subchart's DIRECTORY must not merge: the scope
    # keys on the directory ('_root' for the root), never the Chart.yaml name
    files = {
        "Chart.yaml": "apiVersion: v2\nname: shop\nversion: 1.0.0\n",        # root chart, named 'shop'
        "shop/Chart.yaml": "apiVersion: v2\nname: other\nversion: 1.0.0\n",  # a subchart in dir 'shop'
    }
    q = _ingest(graph, tmp_path, files)
    assert q("MATCH (r:Resource {kind:'chart'}) RETURN count(r)") == [[2]]
    assert q("MATCH (r:Resource {kind:'chart', name:'shop'}) RETURN count(r)") == [[1]]
    assert q("MATCH (r:Resource {kind:'chart', name:'other'}) RETURN count(r)") == [[1]]
