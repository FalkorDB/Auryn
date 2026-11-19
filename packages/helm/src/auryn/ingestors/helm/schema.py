"""Graph schema the Helm producer writes (ADR 0007). A chart `File` (its `Chart.yaml`) owns the chart
`Resource` (the release); the chart DEPENDS_ON the charts it depends on, RUNS_IMAGE the shared `Image`
hub (each container image declared in `values.yaml`), and MOUNTS the notable `ConfigValue`s from
`values.yaml`.

`Image` is the shared hub (MERGE-only; GC'd by the deploy arms). v1 is STATIC and render-free -- only
the YAML-valid `Chart.yaml`/`values.yaml`; a chart's workloads (`Service`/`DEPLOYS`) need a
`helm template` render, the follow-up k8s arm."""

from __future__ import annotations

from auryn.core.graph_model import EdgeSchema, NodeSchema

NODE_SCHEMAS = [
    NodeSchema(label="Project", indexed_properties=("name",)),
    NodeSchema(label="File", indexed_properties=("path",)),
    NodeSchema(label="Resource", indexed_properties=("name",)),      # the chart / release
    NodeSchema(label="ConfigValue", indexed_properties=("name",)),   # a notable value from values.yaml
    NodeSchema(label="Image", indexed_properties=("ref",)),          # the shared hub
]

# Labels this producer OWNS under its `helm://<project>` prefix and diffs on resync. `Image` is shared.
# `Service` is NOT emitted by the static v1, but stays in the reconcilation inventory so a resync of a
# graph first built by the pre-static (#784) template design -- which DID emit helm `Service` workloads
# -- purges those now-orphaned nodes (and their DEPLOYS edges) instead of leaving them behind.
OWNED_LABELS = ["Project", "File", "Resource", "ConfigValue", "Service"]

EDGE_SCHEMAS = [
    EdgeSchema(rel_type="HAS_FILE"),      # Project -> File
    EdgeSchema(rel_type="HAS_RESOURCE"),  # File (Chart.yaml) -> Resource (chart)
    EdgeSchema(rel_type="DEPENDS_ON"),    # Resource (chart) -> Resource (dependency chart)
    EdgeSchema(rel_type="RUNS_IMAGE"),    # Resource (chart) -> Image (a values.yaml image)
    EdgeSchema(rel_type="MOUNTS"),        # Resource (chart) -> ConfigValue (a values.yaml config value)
]
