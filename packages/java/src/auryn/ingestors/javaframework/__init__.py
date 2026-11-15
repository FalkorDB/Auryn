"""Java web-framework enrichment -- a registered graph enrichment (post-pass), not a BaseIngestor or a
CLI command. It runs after the language engines and synthesizes Spring MVC route endpoints (APIEndpoint +
EXPOSES_ENDPOINT) from controller `@*Mapping` annotations, so a Java controller is a first-class HTTP
entry point the cross-language http-endpoints stitch resolves against any-language callers. See
synthesize.py (the Java analogue of pyframework)."""

from auryn.core.registry import GraphEnrichment, register_graph_enrichment
from auryn.ingestors.javaframework.synthesize import run as _run

register_graph_enrichment(GraphEnrichment(name="java-routes", run=_run))

__all__ = ["_run"]
