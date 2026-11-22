"""The `deploy-kustomize` stitch registration -- the `auryn.enrichments` entry-point target (ADR 0007).
Import-light: `stitch` pulls in only core graph helpers (no PyYAML)."""

from __future__ import annotations

from auryn.core.registry import GraphEnrichment, register_graph_enrichment
from auryn.ingestors.kustomize.stitch import run as _run

# order 150 > docker/compose/k8s/terraform/helm arms, so any BUILDS_IMAGE edges already exist when
# image `resolved` is recomputed over the shared hub.
register_graph_enrichment(GraphEnrichment(name="deploy-kustomize", run=_run, order=150))

__all__ = ["_run"]
