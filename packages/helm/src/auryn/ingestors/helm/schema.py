"""Graph schema the Helm producer writes (ADR 0007). A chart `File` (its `Chart.yaml`) owns the chart
`Resource` (the release); the chart DEPENDS_ON the charts it depends on and DEPLOYS the `Service`
workloads its templates declare; each workload RUNS_IMAGE the shared `Image` hub (the image resolved
from `values.yaml`).

`Image` is the shared hub (MERGE-only; GC'd by the deploy arms). v1 is render-free -- the static
`Chart.yaml`/`values.yaml`/template graph; an opt-in `helm template` -> k8s arm is a follow-up."""

from __future__ import annotations

from auryn.core.graph_model import EdgeSchema, NodeSchema

NODE_SCHEMAS = [
    NodeSchema(label="Project", indexed_properties=("name",)),
    NodeSchema(label="File", indexed_properties=("path",)),
    NodeSchema(label="Resource", indexed_properties=("name",)),      # the chart / release
    NodeSchema(label="Service", indexed_properties=("name",)),       # a workload (Deployment/StatefulSet/...)
    NodeSchema(label="Image", indexed_properties=("ref",)),          # the shared hub
]

# Labels this producer OWNS under its `helm://<project>` prefix and diffs on resync. `Image` is shared.
OWNED_LABELS = ["Project", "File", "Resource", "Service"]

EDGE_SCHEMAS = [
    EdgeSchema(rel_type="HAS_FILE"),      # Project -> File
    EdgeSchema(rel_type="HAS_RESOURCE"),  # File (Chart.yaml) -> Resource (chart)
    EdgeSchema(rel_type="DEPENDS_ON"),    # Resource (chart) -> Resource (dependency chart)
    EdgeSchema(rel_type="DEPLOYS"),       # Resource (chart) -> Service (workload)
    EdgeSchema(rel_type="RUNS_IMAGE"),    # Service -> Image
]
