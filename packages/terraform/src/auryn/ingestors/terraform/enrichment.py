"""The `deploy-terraform` stitch registration -- the `auryn.enrichments` entry-point target (ADR 0007).
Import-light: `stitch` pulls in only core graph helpers (no hcl2)."""

from __future__ import annotations

from auryn.core.registry import GraphEnrichment, register_graph_enrichment
from auryn.ingestors.terraform.stitch import run as _run

# order 130 > docker/compose/k8s arms, so BUILDS_IMAGE edges already exist when image `resolved` is
# recomputed over the shared hub.
register_graph_enrichment(GraphEnrichment(name="deploy-terraform", run=_run, order=130))

__all__ = ["_run"]
