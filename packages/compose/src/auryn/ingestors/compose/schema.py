"""Graph schema the Compose producer writes (ADR 0007). A compose `File` owns the `Service`s it
declares (HAS_SERVICE); a service RUNS_IMAGE the shared `Image` hub and DEPENDS_ON other services.

`Project`/`File`/`HAS_FILE` and `Image`/`DEPENDS_ON` match the rest of the tier verbatim (shared
contract). The `BUILDS_IMAGE` edge that closes the build->run loop is synthesized by the
`deploy-compose` enrichment (stitch.py), not the producer -- it joins a service's `build:` to the
Dockerfile's `BuildStage`, so it's declared there, not here."""

from __future__ import annotations

from auryn.core.graph_model import EdgeSchema, NodeSchema

NODE_SCHEMAS = [
    NodeSchema(label="Project", indexed_properties=("name",)),
    NodeSchema(label="File", indexed_properties=("path",)),
    NodeSchema(label="Service", indexed_properties=("name",)),
    NodeSchema(label="Image", indexed_properties=("ref",)),  # the shared hub; `ref` = normalized reference
]

# Labels this producer OWNS under its `compose://<project>` prefix and diffs on resync. `Image` is
# excluded (shared/project-independent `image://...`, MERGE-only; GC'd by the deploy stitch).
OWNED_LABELS = ["Project", "File", "Service"]

EDGE_SCHEMAS = [
    EdgeSchema(rel_type="HAS_FILE"),      # Project -> File
    EdgeSchema(rel_type="HAS_SERVICE"),   # File -> Service
    EdgeSchema(rel_type="RUNS_IMAGE"),    # Service -> Image (the `image:` it runs)
    EdgeSchema(rel_type="DEPENDS_ON"),    # Service -> Service (`depends_on`)
]
