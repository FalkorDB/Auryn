"""The ORM model<->table stitch -- a registered graph enrichment (post-pass), not a BaseIngestor
or a CLI command. It runs after the language ingestors (via core.ingest.run_post_hooks, on both the
full and incremental paths) and joins SQLAlchemy model classes (in a code graph) to the physical
Table/Column nodes a SQL ingestor produced (in a schema graph), by table/column name. See stitch.py."""

from auryn.core.registry import GraphEnrichment, register_graph_enrichment
from auryn.ingestors.orm.stitch import run as _run

register_graph_enrichment(GraphEnrichment(name="orm-tables", run=_run))

__all__ = ["_run"]
