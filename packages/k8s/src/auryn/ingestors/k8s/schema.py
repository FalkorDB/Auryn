"""Graph schema the Kubernetes producer writes (ADR 0007). A manifest `File` owns the `Service`
workloads and `Ingress` routes it declares; a workload RUNS_IMAGE the shared `Image` hub. The
`SERVES` edge (workload -> the route-keyed `APIEndpoint`, the HTTP seam's node) is synthesized by the
`deploy-k8s` enrichment (stitch.py), so it's declared there, not here."""

from __future__ import annotations

from auryn.core.graph_model import EdgeSchema, NodeSchema

NODE_SCHEMAS = [
    NodeSchema(label="Project", indexed_properties=("name",)),
    NodeSchema(label="File", indexed_properties=("path",)),
    NodeSchema(label="Service", indexed_properties=("name",)),       # a workload (Deployment/StatefulSet/...)
    NodeSchema(label="Ingress", indexed_properties=("backend_service",)),
    NodeSchema(label="Image", indexed_properties=("ref",)),          # the shared hub
    NodeSchema(label="APIEndpoint", indexed_properties=("route",)),  # shared with the HTTP seam (ADR 0006)
]

# Labels this producer OWNS under its `k8s://<project>` prefix and diffs on resync. `Image` and
# `APIEndpoint` are shared/project-independent (MERGE-only; GC'd by the deploy / http-endpoints hooks).
OWNED_LABELS = ["Project", "File", "Service", "Ingress"]

EDGE_SCHEMAS = [
    EdgeSchema(rel_type="HAS_FILE"),      # Project -> File
    EdgeSchema(rel_type="HAS_SERVICE"),   # File -> Service (workload)
    EdgeSchema(rel_type="HAS_INGRESS"),   # File -> Ingress
    EdgeSchema(rel_type="RUNS_IMAGE"),    # Service -> Image
]
