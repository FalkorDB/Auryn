"""Graph schema (node/edge labels) the SQL ingestor writes."""

from __future__ import annotations

from auryn.core.graph_model import EdgeSchema, NodeSchema

NODE_SCHEMAS = [
    NodeSchema(label="Database", indexed_properties=("name",)),
    NodeSchema(label="Schema", indexed_properties=("name",)),
    NodeSchema(label="Table", indexed_properties=("name",)),
    NodeSchema(label="Column", indexed_properties=("name",)),
    NodeSchema(label="Index", indexed_properties=("name",)),
    NodeSchema(label="Constraint", indexed_properties=("name",)),
    NodeSchema(label="View", indexed_properties=("name",)),
    NodeSchema(label="Sequence", indexed_properties=("name",)),
]

EDGE_SCHEMAS = [
    EdgeSchema(rel_type="HAS_SCHEMA"),
    EdgeSchema(rel_type="HAS_TABLE"),
    EdgeSchema(rel_type="HAS_COLUMN"),
    EdgeSchema(rel_type="REFERENCES"),
    EdgeSchema(rel_type="HAS_INDEX"),
    EdgeSchema(rel_type="INDEXES"),
    EdgeSchema(rel_type="HAS_CONSTRAINT"),
    EdgeSchema(rel_type="CONSTRAINS"),
    EdgeSchema(rel_type="HAS_VIEW"),
    EdgeSchema(rel_type="HAS_SEQUENCE"),
]
