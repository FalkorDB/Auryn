"""The Kustomize producer against the real `graph` fixture: a kustomization -> Resource + Image, with
DEPENDS_ON (overlay->base) and RUNS_IMAGE (images: transformer), resync, idempotency. Needs FalkorDB."""

from __future__ import annotations

from auryn.core.graph_client import GraphWriter
from auryn.core.local_source import LocalDirectorySource
from auryn.ingestors.kustomize.git_sync import sync_at

_BASE = "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nresources:\n  - deployment.yaml\n"
_OVERLAY = (
    "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\n"
    "resources:\n  - ../../base\n"
    "images:\n  - name: shop/api\n    newTag: \"1.0\"\n"
)
_FILES = {
    "base/kustomization.yaml": _BASE,
    "base/deployment.yaml": "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: api\n",
    "overlays/prod/kustomization.yaml": _OVERLAY,
}


def _ingest(graph, tmp_path, files, project="shop"):
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    src = LocalDirectorySource(str(tmp_path))
    sync_at(GraphWriter(graph), src, src.tree_sha(), project_name=project, repo_uri="test://kustomize", ref=None)
    return lambda c: graph.query(c).result_set


def test_resources_and_containment(graph, tmp_path):
    q = _ingest(graph, tmp_path, _FILES)
    assert q("MATCH (r:Resource {kind:'kustomization'}) RETURN count(r)") == [[2]]  # base + overlay
    assert q("MATCH (:File {name:'kustomization.yaml'})-[:HAS_RESOURCE]->(:Resource) RETURN count(*)") == [[2]]


def test_overlay_runs_the_transformer_image(graph, tmp_path):
    q = _ingest(graph, tmp_path, _FILES)
    assert q("MATCH (:Resource {name:'overlays/prod'})-[:RUNS_IMAGE]->(i:Image) RETURN i.ref") \
        == [["docker.io/shop/api:1.0"]]


def test_overlay_depends_on_base(graph, tmp_path):
    q = _ingest(graph, tmp_path, _FILES)
    # the `resources: [../../base]` resolves to the in-repo base kustomization
    assert q("MATCH (:Resource {name:'overlays/prod'})-[:DEPENDS_ON]->(b:Resource) RETURN b.name") == [["base"]]


def test_file_reference_is_not_a_depends_on(graph, tmp_path):
    # resources pointing at a plain manifest FILE (not a kustomization dir) must not create DEPENDS_ON
    q = _ingest(graph, tmp_path, {
        "base/kustomization.yaml": "kind: Kustomization\nresources:\n  - deployment.yaml\n",
        "base/deployment.yaml": "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: api\n",
    })
    assert q("MATCH (:Resource)-[:DEPENDS_ON]->() RETURN count(*)") == [[0]]


def test_root_kustomization_uses_reserved_scope(graph, tmp_path):
    q = _ingest(graph, tmp_path, {"kustomization.yaml": "kind: Kustomization\nnamespace: default\n"})
    assert q("MATCH (r:Resource) WHERE r.id = 'kustomize://shop/_root' RETURN r.name") == [["_root"]]


def test_resync_removes_deleted_overlay(graph, tmp_path):
    q = _ingest(graph, tmp_path, _FILES)
    assert q("MATCH (r:Resource) RETURN count(r)") == [[2]]
    import shutil
    shutil.rmtree(tmp_path / "overlays")
    q = _ingest(graph, tmp_path, {"base/kustomization.yaml": _BASE,
                                  "base/deployment.yaml": "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: api\n"})
    assert sorted(r[0] for r in q("MATCH (r:Resource) RETURN r.name")) == ["base"]
    assert q("MATCH (:Resource)-[:DEPENDS_ON]->() RETURN count(*)") == [[0]]
    assert q("MATCH (:Resource)-[:RUNS_IMAGE]->() RETURN count(*)") == [[0]]


def test_idempotent_reingest(graph, tmp_path):
    q = _ingest(graph, tmp_path, _FILES)
    before = (q("MATCH (r:Resource) RETURN count(r)"),
              q("MATCH ()-[e:RUNS_IMAGE]->() RETURN count(e)"),
              q("MATCH ()-[e:DEPENDS_ON]->() RETURN count(e)"))
    q = _ingest(graph, tmp_path, _FILES)
    after = (q("MATCH (r:Resource) RETURN count(r)"),
             q("MATCH ()-[e:RUNS_IMAGE]->() RETURN count(e)"),
             q("MATCH ()-[e:DEPENDS_ON]->() RETURN count(e)"))
    assert before == after


def test_repo_with_no_kustomization_is_not_ingested(graph, tmp_path):
    q = _ingest(graph, tmp_path, {
        "k8s/deploy.yaml": "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\n",
        "config.yaml": "foo: bar\n",
    })
    assert q("MATCH (p:Project {name:'shop'}) RETURN count(p)") == [[0]]
    assert q("MATCH (r:Resource) RETURN count(r)") == [[0]]


def test_root_base_reference_resolves(graph, tmp_path):
    # an overlay referencing the REPO-ROOT kustomization (`../..` -> `.` -> the "" key) resolves
    q = _ingest(graph, tmp_path, {
        "kustomization.yaml": "kind: Kustomization\nnamespace: base\n",
        "overlays/prod/kustomization.yaml": "kind: Kustomization\nresources:\n  - ../..\n",
    })
    assert q("MATCH (:Resource {name:'overlays/prod'})-[:DEPENDS_ON]->(r:Resource) RETURN r.name") == [["_root"]]


def test_reserved_root_dir_is_skipped(graph, tmp_path):
    # a kustomization literally in a `_root/` dir collides with the repo-root scope -> skipped
    q = _ingest(graph, tmp_path, {
        "kustomization.yaml": "kind: Kustomization\nnamespace: real-root\n",
        "_root/kustomization.yaml": "kind: Kustomization\nnamespace: impostor\n",
    })
    assert q("MATCH (r:Resource) RETURN count(r)") == [[1]]
    assert q("MATCH (r:Resource) WHERE r.id = 'kustomize://shop/_root' RETURN r.namespace") == [["real-root"]]
