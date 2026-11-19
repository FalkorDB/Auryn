"""The `deploy-k8s` stitch registration -- the `auryn.enrichments` entry-point target (ADR 0007).
Import-light: `stitch` pulls in only core graph helpers (no PyYAML)."""

from __future__ import annotations

from auryn.core.registry import GraphEnrichment, register_graph_enrichment
from auryn.ingestors.k8s.stitch import run as _run

# order 120 > docker `deploy` (100) and compose `deploy-compose` (110), so BUILDS_IMAGE edges from
# those arms already exist when this recomputes image `resolved`.
register_graph_enrichment(GraphEnrichment(name="deploy-k8s", run=_run, order=120))

__all__ = ["_run"]
