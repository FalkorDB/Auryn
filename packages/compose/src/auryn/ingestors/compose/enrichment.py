"""The `deploy-compose` stitch registration -- the `auryn.enrichments` entry-point target (ADR 0007).
Import-light: `stitch` pulls in only core graph helpers (no PyYAML)."""

from __future__ import annotations

from auryn.core.registry import GraphEnrichment, register_graph_enrichment
from auryn.ingestors.compose.stitch import run as _run

# order 110 > the docker `deploy` hook's default 100, so BUILDS_IMAGE is emitted (and `resolved`
# recomputed) AFTER docker's PACKAGES/GC pass -- the final `resolved` reflects the builder this adds.
register_graph_enrichment(GraphEnrichment(name="deploy-compose", run=_run, order=110))

__all__ = ["_run"]
