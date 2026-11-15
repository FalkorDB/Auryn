"""Shared data model every ingestor uses to describe what it writes to the graph."""

from __future__ import annotations

from dataclasses import dataclass, field


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
