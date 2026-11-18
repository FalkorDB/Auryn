"""The `deploy` stitch (ADR 0007): the code seam PACKAGES (BuildStage -> the code it copies in),
image `resolved` recompute, and orphan-image GC. Plus an end-to-end pass through the real ingest
endpoint. Needs FalkorDB (the conftest fixture)."""

from __future__ import annotations

from auryn.core import ingest
from auryn.core.graph_client import GraphWriter
from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.core.local_source import LocalDirectorySource
from auryn.ingestors.docker.git_sync import sync_at
from auryn.ingestors.docker.stitch import _recompute_image_resolved, stitch


def _ingest_docker(graph, tmp_path, files: dict[str, str], project="demo"):
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    src = LocalDirectorySource(str(tmp_path))
    sync_at(GraphWriter(graph), src, src.tree_sha(), project_name=project, repo_uri="test://docker", ref=None)


def _seed_file(writer, lang, rel_path):
    """Simulate a File-emitting code ingestor (bash/go/java/...): a File node keyed `<lang>://...`
    with a repo-relative `path`."""
    writer.merge_node(GraphNode("File", {"id": f"{lang}://demo/file/{rel_path}"}, {"path": rel_path}))


def _seed_module(writer, dotted, file_path):
    """Simulate the Python ingestor: a Module node with `file_path` (no File node)."""
    writer.merge_node(GraphNode("Module", {"id": f"python://demo/module/{dotted}"},
                                {"name": dotted, "file_path": file_path}))


def _seed_file_proj(writer, lang, project, rel_path):
    """A File node under an explicit project -- for multi-repo scoping tests."""
    writer.merge_node(GraphNode("File", {"id": f"{lang}://{project}/file/{rel_path}"}, {"path": rel_path}))


def _q(graph, cypher):
    return graph.query(cypher).result_set


def test_packages_matches_file_and_module(graph, tmp_path):
    """A root Dockerfile with `COPY . /app` packages every code source under its context -- both a
    File-node language and a Python Module."""
    writer = GraphWriter(graph)
    _seed_file(writer, "go", "cmd/main.go")
    _seed_module(writer, "app.main", "app/main.py")
    _ingest_docker(graph, tmp_path, {"Dockerfile": "FROM python:3.12-slim\nCOPY . /app\n"})

    report = stitch(writer)
    assert report is not None and report["packages_edges"] == 2
    packaged = sorted(r[0] for r in _q(graph, "MATCH (:BuildStage)-[:PACKAGES]->(n) RETURN coalesce(n.path, n.file_path)"))
    assert packaged == ["app/main.py", "cmd/main.go"]


def test_packages_scopes_to_copied_subdir(graph, tmp_path):
    """`COPY ./app /app` packages only files under `app/` within the build context, not siblings."""
    writer = GraphWriter(graph)
    _seed_file(writer, "go", "svc/app/main.go")       # under app/ -> packaged
    _seed_file(writer, "go", "svc/tools/gen.go")       # outside app/ -> NOT packaged
    _ingest_docker(graph, tmp_path, {"svc/Dockerfile": "FROM golang:1.22\nCOPY ./app /app\n"})

    stitch(writer)
    packaged = sorted(r[0] for r in _q(graph, "MATCH (:BuildStage)-[:PACKAGES]->(f:File) RETURN f.path"))
    assert packaged == ["svc/app/main.go"]


def test_packages_does_not_cross_link_another_project(graph, tmp_path):
    """In a shared graph holding many repos, a Dockerfile's `COPY .` packages only its OWN repo's
    code -- never another repo's file at the same relative path (the cross-project leak, cubic P1)."""
    writer = GraphWriter(graph)
    _seed_file_proj(writer, "go", "alpha", "app/main.go")   # the Dockerfile's repo
    _seed_file_proj(writer, "go", "beta", "app/main.go")    # an unrelated co-located repo, same path
    _ingest_docker(graph, tmp_path, {"Dockerfile": "FROM scratch\nCOPY . /app\n"}, project="alpha")

    stitch(writer)
    packaged = sorted(r[0] for r in _q(graph, "MATCH (:BuildStage)-[:PACKAGES]->(f:File) RETURN f.id"))
    assert packaged == ["go://alpha/file/app/main.go"]  # only alpha's file, beta is NOT linked


def test_absolute_copy_source_resolves_under_context(graph, tmp_path):
    """`COPY /app /dst` resolves from the build-context ROOT (leading slash dropped), so it packages
    `<context>/app`, not an unmatchable absolute path."""
    writer = GraphWriter(graph)
    _seed_file(writer, "go", "svc/app/main.go")
    _ingest_docker(graph, tmp_path, {"svc/Dockerfile": "FROM scratch\nCOPY /app /dst\n"})
    stitch(writer)
    assert [r[0] for r in _q(graph, "MATCH (:BuildStage)-[:PACKAGES]->(f:File) RETURN f.path")] == ["svc/app/main.go"]


def test_copy_glob_matches_pattern_not_siblings(graph, tmp_path):
    """`COPY src/*.py` packages only the .py files under src/, not a sibling non-match."""
    writer = GraphWriter(graph)
    _seed_file(writer, "py", "svc/src/app.py")      # matches src/*.py
    _seed_file(writer, "py", "svc/src/README.md")   # same dir, does NOT match the glob
    _ingest_docker(graph, tmp_path, {"svc/Dockerfile": "FROM scratch\nCOPY src/*.py /app\n"})
    stitch(writer)
    assert [r[0] for r in _q(graph, "MATCH (:BuildStage)-[:PACKAGES]->(f:File) RETURN f.path")] == ["svc/src/app.py"]


def test_from_stage_copy_is_not_host_package(graph, tmp_path):
    """A `COPY --from=<stage>` pulls from another stage, not the host context -- it must NOT create a
    PACKAGES edge to host files (only a COPIES_FROM stage edge, asserted in the producer tests)."""
    writer = GraphWriter(graph)
    _seed_file(writer, "go", "app/main.go")
    _ingest_docker(graph, tmp_path, {
        "Dockerfile": "FROM golang:1.22 AS build\nFROM scratch\nCOPY --from=build /bin/app /app\n",
    })
    report = stitch(writer)
    # the scratch final stage has only a --from copy -> nothing packaged from the host
    assert report is None or report["packages_edges"] == 0
    assert _q(graph, "MATCH (:BuildStage)-[:PACKAGES]->() RETURN count(*)") == [[0]]


def test_image_resolved_recomputed(graph, tmp_path):
    writer = GraphWriter(graph)
    _ingest_docker(graph, tmp_path, {"Dockerfile": "FROM python:3.12-slim\nCOPY . /app\n"})
    stitch(writer)
    # no builder -> external pull, resolved false
    assert _q(graph, "MATCH (i:Image) RETURN i.resolved, i.origin") == [[False, "registry"]]

    # When a future Compose/CI arm declares this image is built here (a BUILDS_IMAGE edge), the
    # recompute flips resolved->true/origin->internal. Exercised directly: the full stitch owns and
    # clears BUILDS_IMAGE each run (the compose arm would re-emit it), so a synthetic one can't be
    # left lying around for stitch() to see -- but _recompute_image_resolved is the unit under test.
    writer.merge_edge(GraphEdge("BUILDS_IMAGE", "BuildStage", {"id": _any_stage(graph)},
                                "Image", {"id": "image://docker.io/library/python:3.12-slim"},
                                {"provenance": "deploy-stitch"}))
    _recompute_image_resolved(writer)
    assert _q(graph, "MATCH (i:Image) RETURN i.resolved, i.origin") == [[True, "internal"]]


def test_orphan_image_gc(graph, tmp_path):
    writer = GraphWriter(graph)
    _ingest_docker(graph, tmp_path, {"Dockerfile": "FROM python:3.12-slim\nCOPY . /app\n"})
    # an Image with no inbound reference at all (residue of a removed Dockerfile)
    writer.merge_node(GraphNode("Image", {"id": "image://docker.io/library/orphan:1"}, {"ref": "docker.io/library/orphan:1"}))
    report = stitch(writer)
    assert report["orphans_removed"] == 1
    assert _q(graph, "MATCH (i:Image {ref:'docker.io/library/orphan:1'}) RETURN count(i)") == [[0]]
    # the genuinely-referenced base image is NOT GC'd
    assert _q(graph, "MATCH (i:Image {ref:'docker.io/library/python:3.12-slim'}) RETURN count(i)") == [[1]]


def test_stitch_is_idempotent(graph, tmp_path):
    writer = GraphWriter(graph)
    _seed_file(writer, "go", "app/main.go")
    _ingest_docker(graph, tmp_path, {"Dockerfile": "FROM golang:1.22\nCOPY . /app\n"})
    stitch(writer)
    first = _q(graph, "MATCH ()-[r:PACKAGES]->() RETURN count(r)")[0][0]
    stitch(writer)  # rebuilds its own layer -- no duplicates
    assert _q(graph, "MATCH ()-[r:PACKAGES]->() RETURN count(r)")[0][0] == first == 1


def test_end_to_end_through_ingest_endpoint(graph, tmp_path):
    """Full pipeline: the ingest endpoint runs the docker producer + python ingestor + the `deploy`
    enrichment, and PACKAGES connects the build stage to the real Python module."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("def handler():\n    return 'ok'\n")
    (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\nWORKDIR /app\nCOPY . /app\nEXPOSE 8080\n")

    report = ingest.ingest_directory(GraphWriter(graph), str(tmp_path), project_name="demo")
    assert "deploy" in report.enrichments  # the hook ran on the full path

    # the build stage ships the application module
    packaged = _q(graph, "MATCH (:BuildStage)-[:PACKAGES]->(m:Module) RETURN m.file_path")
    assert packaged == [["app/main.py"]]
    # and the base image is the shared hub node
    assert _q(graph, "MATCH (:BuildStage)-[:BASED_ON]->(i:Image) RETURN i.ref") == [["docker.io/library/python:3.12-slim"]]


def _any_stage(graph) -> str:
    return graph.query("MATCH (s:BuildStage) RETURN s.id LIMIT 1").result_set[0][0]
