"""Graph schema the Dockerfile producer writes (ADR 0007). The deployment-tier contract: a
Dockerfile `File` owns its `BuildStage`s (HAS_STAGE); a stage is BASED_ON its base `Image` (or an
earlier stage) and COPIES_FROM earlier stages in a multi-stage build. `Image` is the shared hub
node, keyed by normalized reference and indexed on `ref` so the `deploy` stitch's cross-artifact
joins (and future Compose/k8s `RUNS_IMAGE`) are index seeks.

`Project`/`File`/`HAS_FILE` match the other ingestors verbatim (shared backbone). The code seam
(`PACKAGES`) is synthesized by the `deploy` enrichment (stitch.py), not the producer -- it MERGEs the
edge directly against the existing indexes, so it declares no `EdgeSchema` of its own. `BUILDS_IMAGE`
and the derived `SHIPS` edge are FUTURE contract outputs (the Compose/k8s/Terraform arms, ADR 0007):
the stitch already pre-clears them each run for forward-compat but does not create them yet. All three
are intentionally absent from this producer contract."""

from __future__ import annotations

from auryn.core.graph_model import EdgeSchema, NodeSchema

NODE_SCHEMAS = [
    NodeSchema(label="Project", indexed_properties=("name",)),
    NodeSchema(label="File", indexed_properties=("path",)),
    NodeSchema(label="BuildStage", indexed_properties=("name",)),
    NodeSchema(label="Image", indexed_properties=("ref",)),  # the hub node; `ref` = normalized reference
]

# Labels this producer OWNS under its `docker://<project>` prefix and diffs on resync. `Image` is
# deliberately excluded: image nodes are shared/project-independent (`image://...`), MERGE-only here,
# and GC'd by the `deploy` stitch -- a single project's resync must never delete an image another
# repo still references (the shared-node lifecycle, ADR 0007 §6).
OWNED_LABELS = ["Project", "File", "BuildStage"]

EDGE_SCHEMAS = [
    EdgeSchema(rel_type="HAS_FILE"),      # Project -> File
    EdgeSchema(rel_type="HAS_STAGE"),     # File -> BuildStage
    EdgeSchema(rel_type="BASED_ON"),      # BuildStage -> Image | BuildStage (the FROM chain)
    EdgeSchema(rel_type="COPIES_FROM"),   # BuildStage -> BuildStage (multi-stage COPY --from)
]
