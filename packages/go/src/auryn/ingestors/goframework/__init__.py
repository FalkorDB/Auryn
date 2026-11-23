"""Go HTTP route enrichment -- a registered graph enrichment (post-pass), not a BaseIngestor or a CLI
command. It runs after the language engines and turns the route registrations the Go parser recorded
(`http_routes` on the registering Function/Method: net/http, gin, echo, chi, gorilla/mux) into
route-keyed `APIEndpoint`s exposed by their handlers, so a Go service is an HTTP entry point the
cross-language http-endpoints stitch resolves against any-language callers. See synthesize.py (the Go
analogue of pyframework and javaframework)."""

from auryn.core.registry import GraphEnrichment, register_graph_enrichment
from auryn.ingestors.goframework.synthesize import run as _run

register_graph_enrichment(GraphEnrichment(name="go-routes", run=_run))

__all__ = ["_run"]
