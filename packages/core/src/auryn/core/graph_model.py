"""Shared data model every ingestor uses to describe what it writes to the graph."""

from __future__ import annotations

from dataclasses import dataclass, field

# Generation of the graph's label / relationship-type vocabulary. BUMP this whenever a schema change
# is NOT backward-compatible with an already-ingested graph -- a label/rel-type rename, split, or
# removal (e.g. #849's Typedef->TypeAlias) -- so an incremental resync would leave stale, now-orphaned
# nodes. Additive changes (a brand-new label/edge) do NOT need a bump. The running Auryn stamps this
# version per machine (app_config.Config.schema_version); on finding an older stamp it migrates
# incompatible local graphs (delete + re-ingest) rather than silently serving a half-renamed graph
# (#851). Detection is purely this version comparison -- never a scan of the graph's labels.
GRAPH_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class NodeSchema:
    """Declares a node label an ingestor writes, so setup_schema() can index/constrain it."""

    label: str
    key_property: str = "id"
    indexed_properties: tuple[str, ...] = ()


@dataclass(frozen=True)
class EdgeSchema:
    """Declares an edge type an ingestor writes."""

    rel_type: str


@dataclass(frozen=True)
class GraphNode:
    label: str
    key: dict[str, object]
    properties: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class GraphEdge:
    rel_type: str
    from_label: str
    from_key: dict[str, object]
    to_label: str
    to_key: dict[str, object]
    properties: dict[str, object] = field(default_factory=dict)
