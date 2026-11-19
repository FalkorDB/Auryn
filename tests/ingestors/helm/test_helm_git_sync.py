"""The Helm producer against the real `graph` fixture: a chart -> Resource (release) + ConfigValue +
Image, with DEPENDS_ON/RUNS_IMAGE/MOUNTS edges, resync, and idempotency. Needs FalkorDB (conftest)."""

from __future__ import annotations

from auryn.core.graph_client import GraphWriter
from auryn.core.local_source import LocalDirectorySource
from auryn.ingestors.helm.git_sync import sync_at

_CHART = "apiVersion: v2\nname: shop-api\nversion: 1.0.0\ntype: application\ndependencies:\n  - name: redis\n    version: \"17.x\"\n"
_VALUES = "image:\n  repository: shop/api\n  tag: \"1.0\"\nreplicaCount: 2\n"

_CHART_FILES = {
    "charts/shop/Chart.yaml": _CHART,
    "charts/shop/values.yaml": _VALUES,
    # a Go-templated manifest: must be IGNORED by helm (not valid YAML, no render in v1)
    "charts/shop/templates/deployment.yaml":
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: {{ include \"x\" . }}\n",
}


def _ingest(graph, tmp_path, files, project="shop"):
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    src = LocalDirectorySource(str(tmp_path))
    sync_at(GraphWriter(graph), src, src.tree_sha(), project_name=project, repo_uri="test://helm", ref=None)
    return lambda c: graph.query(c).result_set


def test_chart_resource_and_containment(graph, tmp_path):
    q = _ingest(graph, tmp_path, _CHART_FILES)
    # the chart is a `release`-classified Resource
    assert q("MATCH (r:Resource {kind:'release', name:'shop-api'}) RETURN r.resolved, r.chart_type") == [[True, "application"]]
    assert q("MATCH (:File {name:'Chart.yaml'})-[:HAS_RESOURCE]->(:Resource {name:'shop-api'}) RETURN count(*)") == [[1]]
    # both the Chart.yaml and values.yaml files are in the graph (project -> file containment)
    assert sorted(r[0] for r in q("MATCH (:Project)-[:HAS_FILE]->(f:File) RETURN f.name")) == ["Chart.yaml", "values.yaml"]


def test_no_service_in_v1(graph, tmp_path):
    # the Go-templated template is not parsed -> no Service / DEPLOYS (render-free)
    q = _ingest(graph, tmp_path, _CHART_FILES)
    assert q("MATCH (s:Service) RETURN count(s)") == [[0]]
    assert q("MATCH ()-[d:DEPLOYS]->() RETURN count(d)") == [[0]]


def test_runs_image_joins_hub(graph, tmp_path):
    q = _ingest(graph, tmp_path, _CHART_FILES)
    assert q("MATCH (:Resource {name:'shop-api'})-[:RUNS_IMAGE]->(i:Image) RETURN i.ref, i.resolved") \
        == [["docker.io/shop/api:1.0", False]]


def test_config_values_mounted(graph, tmp_path):
    q = _ingest(graph, tmp_path, _CHART_FILES)
    cvs = {r[0]: r[1] for r in q("MATCH (:Resource {name:'shop-api'})-[:MOUNTS]->(c:ConfigValue) RETURN c.name, c.value")}
    assert cvs["replicaCount"] == "2"
    assert cvs["image.repository"] == "shop/api"


def test_external_dependency_is_a_placeholder(graph, tmp_path):
    q = _ingest(graph, tmp_path, _CHART_FILES)
    assert q("MATCH (:Resource {name:'shop-api'})-[:DEPENDS_ON]->(d:Resource) RETURN d.name, d.kind, d.resolved") \
        == [["redis", "chart", False]]


def test_in_repo_dependency_resolves_to_the_chart(graph, tmp_path):
    files = dict(_CHART_FILES)
    files["charts/shop/Chart.yaml"] = "apiVersion: v2\nname: shop-api\nversion: 1.0.0\ndependencies:\n  - name: cache\n"
    files["charts/cache/Chart.yaml"] = "apiVersion: v2\nname: cache\nversion: 2.0.0\n"
    q = _ingest(graph, tmp_path, files)
    # the dep resolves to the REAL in-repo chart (kind release, resolved:true), not a placeholder
    assert q("MATCH (:Resource {name:'shop-api'})-[:DEPENDS_ON]->(d:Resource {name:'cache'}) RETURN d.kind, d.resolved") == [["release", True]]
    assert q("MATCH (d:Resource {name:'cache', resolved:false}) RETURN count(d)") == [[0]]


def test_resync_removes_deleted_chart(graph, tmp_path):
    q = _ingest(graph, tmp_path, _CHART_FILES)
    assert q("MATCH (r:Resource) RETURN count(r)") == [[2]]  # shop-api + the redis placeholder
    # remove the whole chart -> its Resource/ConfigValue/File + the dep placeholder disappear
    import shutil
    shutil.rmtree(tmp_path / "charts" / "shop")
    q = _ingest(graph, tmp_path, {"charts/cache/Chart.yaml": "apiVersion: v2\nname: cache\nversion: 2.0.0\n"})
    assert sorted(r[0] for r in q("MATCH (r:Resource) RETURN r.name")) == ["cache"]
    assert q("MATCH (c:ConfigValue) RETURN count(c)") == [[0]]
    assert q("MATCH ()-[d:DEPENDS_ON]->() RETURN count(d)") == [[0]]


def test_idempotent_reingest(graph, tmp_path):
    q = _ingest(graph, tmp_path, _CHART_FILES)
    before = {
        "resources": q("MATCH (r:Resource) RETURN count(r)"),
        "config": q("MATCH (c:ConfigValue) RETURN count(c)"),
        "runs": q("MATCH ()-[e:RUNS_IMAGE]->() RETURN count(e)"),
        "mounts": q("MATCH ()-[e:MOUNTS]->() RETURN count(e)"),
        "depends": q("MATCH ()-[e:DEPENDS_ON]->() RETURN count(e)"),
    }
    q = _ingest(graph, tmp_path, _CHART_FILES)  # same content again
    after = {
        "resources": q("MATCH (r:Resource) RETURN count(r)"),
        "config": q("MATCH (c:ConfigValue) RETURN count(c)"),
        "runs": q("MATCH ()-[e:RUNS_IMAGE]->() RETURN count(e)"),
        "mounts": q("MATCH ()-[e:MOUNTS]->() RETURN count(e)"),
        "depends": q("MATCH ()-[e:DEPENDS_ON]->() RETURN count(e)"),
    }
    assert before == after


def test_repo_with_no_chart_is_not_ingested(graph, tmp_path):
    # a plain k8s manifest + a compose file, NO Chart.yaml -> no phantom helm:// project
    q = _ingest(graph, tmp_path, {
        "k8s/deploy.yaml": "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\n",
        "docker-compose.yaml": "services:\n  web:\n    image: nginx:1.25\n",
    })
    assert q("MATCH (p:Project {name:'shop'}) RETURN count(p)") == [[0]]
    assert q("MATCH (r:Resource) RETURN count(r)") == [[0]]


def test_root_chart_uses_reserved_scope_not_its_name(graph, tmp_path):
    # a ROOT chart named `shop` and a subdir chart at `shop/` must NOT merge into one Resource: the root
    # keys on the reserved `_root` segment, NEVER its Chart.yaml name, so they stay distinct.
    q = _ingest(graph, tmp_path, {
        "Chart.yaml": "apiVersion: v2\nname: shop\nversion: 1.0.0\n",
        "shop/Chart.yaml": "apiVersion: v2\nname: shop-sub\nversion: 2.0.0\n",
    })
    assert q("MATCH (r:Resource {kind:'release'}) RETURN count(r)") == [[2]]
    assert sorted(r[0] for r in q("MATCH (r:Resource {kind:'release'}) RETURN r.chart_dir")) == ["", "shop"]
    assert q("MATCH (r:Resource) WHERE r.id = 'helm://shop/_root' RETURN r.name") == [["shop"]]


def test_chart_in_reserved_dir_is_skipped(graph, tmp_path):
    # a chart whose directory is the reserved `_dep/` namespace is skipped (it could otherwise collide
    # with a dependency placeholder); a normal sibling chart still ingests.
    q = _ingest(graph, tmp_path, {
        "charts/shop/Chart.yaml": "apiVersion: v2\nname: shop\nversion: 1.0.0\n",
        "_dep/evil/Chart.yaml": "apiVersion: v2\nname: evil\nversion: 1.0.0\n",
    })
    assert q("MATCH (r:Resource {name:'shop'}) RETURN count(r)") == [[1]]
    assert q("MATCH (r:Resource {name:'evil'}) RETURN count(r)") == [[0]]


def test_ambiguous_dependency_stays_a_placeholder(graph, tmp_path):
    # two in-repo charts share the name `common`; a dependency on `common` can't pick one, so it stays
    # an unresolved placeholder rather than resolving arbitrarily to one of them.
    q = _ingest(graph, tmp_path, {
        "charts/a/Chart.yaml": "apiVersion: v2\nname: common\nversion: 1.0.0\n",
        "charts/b/Chart.yaml": "apiVersion: v2\nname: common\nversion: 2.0.0\n",
        "charts/app/Chart.yaml": "apiVersion: v2\nname: app\nversion: 1.0.0\ndependencies:\n  - name: common\n",
    })
    assert q("MATCH (:Resource {name:'app'})-[:DEPENDS_ON]->(d) RETURN d.id") == [["helm://shop/_dep/common"]]
    assert q("MATCH (:Resource {name:'app'})-[:DEPENDS_ON]->(d:Resource {resolved:false}) RETURN count(d)") == [[1]]


def test_sensitive_config_value_is_redacted(graph, tmp_path):
    # a credential in values.yaml must never be stored verbatim (graph/viewer/MCP surface the value)
    q = _ingest(graph, tmp_path, {
        "charts/shop/Chart.yaml": "apiVersion: v2\nname: shop\nversion: 1.0.0\n",
        "charts/shop/values.yaml": "replicaCount: 2\ndatabase:\n  password: s3cr3t\n",
    })
    assert q("MATCH (c:ConfigValue {name:'database.password'}) RETURN c.value, c.sensitive") == [["[redacted]", True]]
    assert q("MATCH (c:ConfigValue {name:'replicaCount'}) RETURN c.value, c.sensitive") == [["2", False]]


def test_nameless_chart_is_unresolved(graph, tmp_path):
    # a Chart.yaml with no name still anchors a Resource, but marked resolved:false (not a valid chart)
    q = _ingest(graph, tmp_path, {"charts/shop/Chart.yaml": "apiVersion: v2\nversion: 1.0.0\n"})
    assert q("MATCH (r:Resource {kind:'release'}) RETURN r.resolved") == [[False]]
