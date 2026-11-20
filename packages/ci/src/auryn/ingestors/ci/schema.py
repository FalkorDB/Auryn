"""Graph schema the CI producer writes (ADR 0007). A workflow `File` owns a `Resource` (the CI
pipeline); the workflow BUILDS_IMAGE the shared `Image` hub for each image its steps build or push.

`Image` is the shared hub (MERGE-only; GC'd by the deploy arms). `BUILDS_IMAGE` is the edge the docker
producer deliberately leaves to CI/Compose (a Dockerfile doesn't name the image it produces) -- it is
what flips a hub image `resolved`/`internal`, closing the build->image->run loop. v1 is static -- the
workflow YAML only. *Future:* `Registry` + `STORED_IN` (login/push targets); `DEPLOYS` (deploy steps)."""

from __future__ import annotations

from auryn.core.graph_model import EdgeSchema, NodeSchema

NODE_SCHEMAS = [
    NodeSchema(label="Project", indexed_properties=("name",)),
    NodeSchema(label="File", indexed_properties=("path",)),
    NodeSchema(label="Resource", indexed_properties=("name",)),      # the CI workflow/pipeline
    NodeSchema(label="Image", indexed_properties=("ref",)),          # the shared hub
]

# Labels this producer OWNS under its `ci://<project>` prefix and diffs on resync. `Image` is shared.
OWNED_LABELS = ["Project", "File", "Resource"]

EDGE_SCHEMAS = [
    EdgeSchema(rel_type="HAS_FILE"),       # Project -> File
    EdgeSchema(rel_type="HAS_RESOURCE"),   # File (workflow) -> Resource
    EdgeSchema(rel_type="BUILDS_IMAGE"),   # Resource (workflow) -> Image (built/pushed)
]
