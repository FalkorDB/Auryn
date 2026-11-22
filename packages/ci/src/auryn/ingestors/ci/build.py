"""Turn parsed CI workflows into the deployment-tier graph (ADR 0007): a workflow `File` owns a
`Resource` (the CI pipeline); the workflow BUILDS_IMAGE the shared `Image` hub for each image its steps
build or push. That `BUILDS_IMAGE` is what flips a hub image `resolved`/`internal` -- closing the
build->image->run loop another producer's `RUNS_IMAGE` only half-expressed.

Emits no cross-artifact edge beyond the shared `Image` hub; the `deploy-ci` enrichment runs the image
lifecycle. v1 is static -- no execution."""

from __future__ import annotations

import posixpath
from collections.abc import Iterator

from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.core.image_ref import image_id, image_node
from auryn.ingestors.ci import ids
from auryn.ingestors.ci.model import ParsedWorkflow


def build_graph(workflows: list[tuple[str, ParsedWorkflow]], proj_id: str) -> Iterator[GraphNode | GraphEdge]:
    for file_path, pw in workflows:
        wid = ids.workflow_id(proj_id, file_path)
        fid = ids.file_id(proj_id, file_path)
        name = pw.name or posixpath.basename(file_path)

        yield GraphNode("File", {"id": fid}, {"path": file_path, "name": posixpath.basename(file_path)})
        yield GraphEdge("HAS_FILE", "Project", {"id": proj_id}, "File", {"id": fid})
        yield GraphNode("Resource", {"id": wid}, {
            "name": name, "kind": "ci-workflow", "path": file_path, "resolved": pw.valid,
        })
        yield GraphEdge("HAS_RESOURCE", "File", {"id": fid}, "Resource", {"id": wid})

        for ref in pw.built_images:
            yield image_node(ref)
            yield GraphEdge("BUILDS_IMAGE", "Resource", {"id": wid}, "Image", {"id": image_id(ref)},
                            {"provenance": "ci"})
