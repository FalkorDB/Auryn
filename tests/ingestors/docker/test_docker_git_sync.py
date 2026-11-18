"""The Docker producer against the real `graph` fixture: Dockerfiles -> Project/File/BuildStage/Image
nodes + HAS_FILE/HAS_STAGE/BASED_ON/COPIES_FROM edges, the shared-image hub, and resync delta.
Needs FalkorDB (the conftest fixture)."""

from __future__ import annotations

from auryn.core.graph_client import GraphWriter
from auryn.core.local_source import LocalDirectorySource
from auryn.ingestors.docker.git_sync import sync_at

_MULTISTAGE = (
    "FROM python:3.12-slim AS base\n"
    "WORKDIR /app\n"
    "COPY requirements.txt .\n"
    "FROM base AS final\n"
    "COPY . /app\n"
    "COPY --from=base /usr/local /usr/local\n"
    "EXPOSE 8080\n"
    'ENTRYPOINT ["python", "-m", "app"]\n'
)


def _ingest(graph, tmp_path, files: dict[str, str], project="demo"):
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    src = LocalDirectorySource(str(tmp_path))
    sync_at(GraphWriter(graph), src, src.tree_sha(), project_name=project, repo_uri="test://docker", ref=None)

    def q(cypher):
        return graph.query(cypher).result_set

    return q


def test_stages_files_and_containment(graph, tmp_path):
    q = _ingest(graph, tmp_path, {"Dockerfile": _MULTISTAGE})
    assert sorted(r[0] for r in q("MATCH (s:BuildStage) RETURN s.stage")) == ["base", "final"]
    # the Dockerfile is a File owned by the Project, and owns its stages
    assert q("MATCH (:Project)-[:HAS_FILE]->(f:File) RETURN f.path") == [["Dockerfile"]]
    assert q("MATCH (:File)-[:HAS_STAGE]->(s:BuildStage) RETURN count(s)") == [[2]]


def test_based_on_external_image(graph, tmp_path):
    q = _ingest(graph, tmp_path, {"Dockerfile": _MULTISTAGE})
    # stage `base` is BASED_ON the external python image (a shared, resolved:false hub node)
    rows = q("MATCH (s:BuildStage {stage:'base'})-[:BASED_ON]->(i:Image) RETURN i.ref, i.resolved, i.origin")
    assert rows == [["docker.io/library/python:3.12-slim", False, "registry"]]


def test_multistage_internal_based_on_and_copies_from(graph, tmp_path):
    q = _ingest(graph, tmp_path, {"Dockerfile": _MULTISTAGE})
    # final is BASED_ON the earlier `base` stage (internal), not an Image
    assert q("MATCH (:BuildStage {stage:'final'})-[:BASED_ON]->(b:BuildStage) RETURN b.stage") == [["base"]]
    # and COPIES_FROM it (the multi-stage COPY --from=base)
    assert q("MATCH (:BuildStage {stage:'final'})-[:COPIES_FROM]->(b:BuildStage) RETURN b.stage") == [["base"]]


def test_exposed_ports_and_entrypoint_recorded(graph, tmp_path):
    q = _ingest(graph, tmp_path, {"Dockerfile": _MULTISTAGE})
    assert q("MATCH (s:BuildStage {stage:'final'}) RETURN s.exposed_ports") == [[["8080"]]]
    assert q("MATCH (s:BuildStage {stage:'final'}) RETURN s.entrypoint") == [['["python", "-m", "app"]']]


def test_shared_base_image_is_one_node(graph, tmp_path):
    """Two Dockerfiles with the same base collapse onto ONE Image node (the shared-base signal)."""
    q = _ingest(graph, tmp_path, {
        "api/Dockerfile": "FROM python:3.12-slim\nCOPY . /app\n",
        "worker/Dockerfile": "FROM python:3.12-slim\nCOPY . /app\n",
    })
    assert q("MATCH (i:Image {ref:'docker.io/library/python:3.12-slim'}) RETURN count(i)") == [[1]]
    # both stages point at that single node
    assert q("MATCH (:BuildStage)-[:BASED_ON]->(i:Image {ref:'docker.io/library/python:3.12-slim'}) "
             "RETURN count(*)") == [[2]]


def test_scratch_has_no_base_image(graph, tmp_path):
    q = _ingest(graph, tmp_path, {"Dockerfile": "FROM scratch\nCOPY bin/app /app\n"})
    assert q("MATCH (:BuildStage)-[:BASED_ON]->() RETURN count(*)") == [[0]]
    assert q("MATCH (i:Image) RETURN count(i)") == [[0]]


def test_resync_removes_deleted_stage(graph, tmp_path):
    q = _ingest(graph, tmp_path, {"Dockerfile": _MULTISTAGE})
    assert q("MATCH (s:BuildStage) RETURN count(s)") == [[2]]
    # drop the second stage -> resync removes it and its edges (recompute-and-delta)
    q = _ingest(graph, tmp_path, {"Dockerfile": "FROM python:3.12-slim AS base\nCOPY . /app\n"})
    assert sorted(r[0] for r in q("MATCH (s:BuildStage) RETURN s.stage")) == ["base"]
    assert q("MATCH (:BuildStage)-[:COPIES_FROM]->() RETURN count(*)") == [[0]]


def test_resync_changed_base_removes_stale_edge(graph, tmp_path):
    # Changing `FROM alpine:3.20` -> `3.21` must drop the old BASED_ON edge, not keep both. Exercises
    # that edges to the shared `image://` hub are diffed on recompute (graph_client edge fingerprints).
    q = _ingest(graph, tmp_path, {"Dockerfile": "FROM alpine:3.20\nCOPY . /app\n"})
    assert sorted(r[0] for r in q("MATCH (:BuildStage)-[:BASED_ON]->(i:Image) RETURN i.ref")) == ["docker.io/library/alpine:3.20"]
    q = _ingest(graph, tmp_path, {"Dockerfile": "FROM alpine:3.21\nCOPY . /app\n"})
    assert sorted(r[0] for r in q("MATCH (:BuildStage)-[:BASED_ON]->(i:Image) RETURN i.ref")) == ["docker.io/library/alpine:3.21"]


def _snapshot(graph):
    """The full deployment state: node id set, edge (type, from, to) triples, and the key props of
    stages/images -- so a replaced-with-wrong edge or a changed stage property is caught, not just a
    stable count. (The Project's own `commit_sha` deliberately differs sha-A->sha-B, so it's excluded.)"""
    q = lambda c: graph.query(c).result_set
    nodes = sorted(r[0] for r in q("MATCH (n) WHERE n.id IS NOT NULL RETURN n.id"))
    edges = sorted((r[0], r[1], r[2]) for r in q(
        "MATCH (a)-[r]->(b) WHERE a.id IS NOT NULL AND b.id IS NOT NULL RETURN type(r), a.id, b.id"))
    props = sorted(tuple(r) for r in q("MATCH (s:BuildStage) RETURN s.id, s.base_ref, s.stage, s.copy_sources")) \
        + sorted(tuple(r) for r in q("MATCH (i:Image) RETURN i.id, i.ref, i.resolved"))
    return nodes, edges, props


def test_resync_recompute_is_idempotent(graph, tmp_path):
    # Re-applying the SAME content under a DIFFERENT target_sha reaches the recompute+delta path (not
    # the commit_sha short-circuit) and must leave the graph identical -- same nodes, edges, and props.
    (tmp_path / "Dockerfile").write_text(_MULTISTAGE)
    src = LocalDirectorySource(str(tmp_path))
    w = GraphWriter(graph)
    sync_at(w, src, "sha-A", project_name="demo", repo_uri="test://docker", ref=None)
    before = _snapshot(graph)
    sync_at(w, src, "sha-B", project_name="demo", repo_uri="test://docker", ref=None)  # diff sha -> recompute
    assert _snapshot(graph) == before


def test_arg_without_default_keeps_unresolved_base(graph, tmp_path):
    # `ARG BASE` (no default) + `FROM ${BASE}` must NOT drop the base to an empty ref -- it stays an
    # unresolved Image placeholder, not silently omitted.
    q = _ingest(graph, tmp_path, {"Dockerfile": "ARG BASE\nFROM ${BASE}\nCOPY . /app\n"})
    assert q("MATCH (:BuildStage)-[:BASED_ON]->(i:Image) RETURN count(i)") == [[1]]
    assert q("MATCH (:BuildStage)-[:BASED_ON]->(i:Image) RETURN i.resolved") == [[False]]
