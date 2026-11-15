"""The HTTP call<->endpoint stitch -- a registered graph enrichment (post-pass), not a
BaseIngestor or a CLI command. It runs after the language ingestors (via
core.ingest.run_post_hooks, on both the full and incremental paths) and resolves route-keyed
APIEndpoint placeholders to their handler Methods across the shared graph. See stitch.py."""

from auryn.core.registry import GraphEnrichment, register_graph_enrichment
from auryn.ingestors.http.stitch import run as _run

# order=200 (> the default 100): the stitch is the RESOLVER -- it must run AFTER every endpoint-DECLARATION
# enrichment (pyframework's `python-routes`, javaframework's `java-routes`, ...) so their APIEndpoints
# already exist when it matches callers to handlers. With the default order, `http-endpoints` sorted
# alphabetically BEFORE `java-routes`/`python-routes` and ran first, so a route declared by an enrichment
# (not by a language ingestor, e.g. C#) wouldn't resolve to its callers on a fresh single-pass ingest.
register_graph_enrichment(GraphEnrichment(name="http-endpoints", run=_run, order=200))

__all__ = ["_run"]
