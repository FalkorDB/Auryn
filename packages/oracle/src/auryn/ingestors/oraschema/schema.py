"""Graph schema the `oraschema` ingestor writes.

Deliberately the live `sql` ingestor's node/edge set (same labels, same indexed
properties) so a file-reconstructed schema and a live-reflected one are the *same*
subgraph -- plus a `READS` edge, which the file path can derive from a view's SELECT
(the generic Inspector the live `sql` ingestor uses can't, so it's additive here).
"""

from __future__ import annotations

from auryn.core.graph_model import EdgeSchema
from auryn.ingestors.sql.schema import EDGE_SCHEMAS as _SQL_EDGE_SCHEMAS
from auryn.ingestors.sql.schema import NODE_SCHEMAS as _SQL_NODE_SCHEMAS

NODE_SCHEMAS = list(_SQL_NODE_SCHEMAS)

# View -> Table read edges are parsed from the view's SELECT; the live `sql` ingestor
# leaves that as a known gap, so `READS` is the one edge type this ingestor adds.
EDGE_SCHEMAS = [*_SQL_EDGE_SCHEMAS, EdgeSchema(rel_type="READS")]
