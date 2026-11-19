"""Graph schema the Kustomize producer writes (ADR 0007). A `kustomization.yaml` `File` owns a
`Resource` (the overlay/base); the overlay RUNS_IMAGE the shared `Image` hub for each ref its
`images:` transformer sets, and DEPENDS_ON the base kustomizations it layers on (`resources:`/`bases:`
/`components:` that resolve to another in-repo kustomization).

`Image` is the shared hub (MERGE-only; GC'd by the deploy arms). v1 is STATIC and render-free -- only
the `kustomization.yaml` is read; `kustomize build` (rendering the patched base manifests) is a
follow-up."""

from __future__ import annotations

from auryn.core.graph_model import EdgeSchema, NodeSchema

NODE_SCHEMAS = [
    NodeSchema(label="Project", indexed_properties=("name",)),
    NodeSchema(label="File", indexed_properties=("path",)),
    NodeSchema(label="Resource", indexed_properties=("name",)),      # the kustomization (overlay/base)
    NodeSchema(label="Image", indexed_properties=("ref",)),          # the shared hub
]

# Labels this producer OWNS under its `kustomize://<project>` prefix and diffs on resync. `Image` is shared.
OWNED_LABELS = ["Project", "File", "Resource"]

EDGE_SCHEMAS = [
    EdgeSchema(rel_type="HAS_FILE"),      # Project -> File
    EdgeSchema(rel_type="HAS_RESOURCE"),  # File (kustomization.yaml) -> Resource
    EdgeSchema(rel_type="DEPENDS_ON"),    # Resource (overlay) -> Resource (base kustomization)
    EdgeSchema(rel_type="RUNS_IMAGE"),    # Resource -> Image (an `images:` transformer entry)
]
