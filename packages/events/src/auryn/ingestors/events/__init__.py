"""The message/event-bus stitch -- a registered graph enrichment (post-pass), not a BaseIngestor or a
CLI command. It runs after the language ingestors (via core.ingest.run_post_hooks, full + incremental)
and ties a publisher to a subscriber through the topic/queue they share -- a decoupled connection that
has no call edge in the graph. See stitch.py."""

from auryn.core.registry import GraphEnrichment, register_graph_enrichment
from auryn.ingestors.events.stitch import run as _run

register_graph_enrichment(GraphEnrichment(name="message-bus", run=_run))

__all__ = ["_run"]
