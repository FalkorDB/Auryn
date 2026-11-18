"""Graph schema the Terraform producer writes (ADR 0007). A `.tf` `File` owns the `Resource` blocks it
declares; resources `DEPENDS_ON` the resources they reference, `RUNS_IMAGE` the shared `Image` hub (a
container image in the resource), and `PROVISIONS` the `Database`/`Registry` they create.

`Image` is the shared hub (MERGE-only; GC'd by the deploy arms). `Database`/`Registry` are declared
infra the ORM/registry seams can later join to."""

from __future__ import annotations

from auryn.core.graph_model import EdgeSchema, NodeSchema

NODE_SCHEMAS = [
    NodeSchema(label="Project", indexed_properties=("name",)),
    NodeSchema(label="File", indexed_properties=("path",)),
    NodeSchema(label="Resource", indexed_properties=("address",)),
    NodeSchema(label="Image", indexed_properties=("ref",)),      # the shared hub
    NodeSchema(label="Database", indexed_properties=("name",)),
    NodeSchema(label="Registry", indexed_properties=("name",)),
]

# Labels this producer OWNS under `terraform://<project>` and diffs on resync. `Image` is shared.
OWNED_LABELS = ["Project", "File", "Resource", "Database", "Registry"]

EDGE_SCHEMAS = [
    EdgeSchema(rel_type="HAS_FILE"),      # Project -> File
    EdgeSchema(rel_type="HAS_RESOURCE"),  # File -> Resource
    EdgeSchema(rel_type="DEPENDS_ON"),    # Resource -> Resource (interpolation reference)
    EdgeSchema(rel_type="RUNS_IMAGE"),    # Resource -> Image
    EdgeSchema(rel_type="PROVISIONS"),    # Resource -> Database | Registry
]
