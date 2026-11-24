"""The `deploy-ci` stitch registration -- the `auryn.enrichments` entry-point target (ADR 0007).
Import-light: `stitch` pulls in only core graph helpers (no PyYAML)."""

from __future__ import annotations

from auryn.core.registry import GraphEnrichment, register_graph_enrichment
from auryn.ingestors.ci.stitch import run as _run

# order 160 > the IaC arms. CI WRITES `BUILDS_IMAGE`, so this arm's `recompute_image_resolved` is what
# flips a hub image internal; running late means every producer's RUNS_IMAGE already exists.
register_graph_enrichment(GraphEnrichment(name="deploy-ci", run=_run, order=160))

__all__ = ["_run"]
