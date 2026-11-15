"""Python web-framework enrichment -- a registered graph enrichment (post-pass), not a
BaseIngestor or a CLI command. It runs after the language engines (via
core.ingest.ingest_repository/ingest_directory) and synthesizes Python route endpoints
(APIEndpoint + EXPOSES_ENDPOINT) from route decorators. See synthesize.py."""

from auryn.core.registry import GraphEnrichment, register_graph_enrichment
from auryn.ingestors.pyframework.synthesize import run as _run

register_graph_enrichment(GraphEnrichment(name="python-routes", run=_run))

__all__ = ["_run"]
