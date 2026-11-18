"""The `deploy` stitch registration -- the `auryn.enrichments` entry-point target (ADR 0007 §7).

Kept separate from plugin.py (the GitLanguage) so the two roles register independently, and
import-light: `stitch` pulls in only core graph helpers (no parser), so enrichment discovery never
imports the Dockerfile engine."""

from __future__ import annotations

from auryn.core.registry import GraphEnrichment, register_graph_enrichment
from auryn.ingestors.docker.stitch import run as _run

register_graph_enrichment(GraphEnrichment(name="deploy", run=_run))

__all__ = ["_run"]
