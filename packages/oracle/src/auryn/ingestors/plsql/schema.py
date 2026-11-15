"""Graph schema (node/edge labels) the PL/SQL ingestor writes.

Reuses the SQL ingestor's Database/Schema/Table id scheme (see
ingestors/sql/ingestor.py) rather than declaring those labels again here --
this ingestor only ever attaches to them (HAS_TRIGGER, CALLS, READS, WRITES,
DEPENDS_ON), it doesn't own them.
"""

from __future__ import annotations

from auryn.core.graph_model import EdgeSchema, NodeSchema

NODE_SCHEMAS = [
    NodeSchema(label="Package", indexed_properties=("name",)),
    NodeSchema(label="Procedure", indexed_properties=("name",)),
    NodeSchema(label="Function", indexed_properties=("name",)),
    NodeSchema(label="Parameter", indexed_properties=("name",)),
    NodeSchema(label="Trigger", indexed_properties=("name",)),
    NodeSchema(label="Type", indexed_properties=("name",)),
]

EDGE_SCHEMAS = [
    EdgeSchema(rel_type="HAS_PACKAGE"),
    EdgeSchema(rel_type="HAS_PROCEDURE"),
    EdgeSchema(rel_type="HAS_FUNCTION"),
    EdgeSchema(rel_type="HAS_PARAMETER"),
    EdgeSchema(rel_type="HAS_TRIGGER"),
    EdgeSchema(rel_type="HAS_TYPE"),
    EdgeSchema(rel_type="CALLS"),
    EdgeSchema(rel_type="READS"),
    EdgeSchema(rel_type="WRITES"),
    EdgeSchema(rel_type="DEPENDS_ON"),
]

# The file-based `plsql` ingestor writes the same Package/Procedure/Function/
# Parameter/Trigger/Type labels as the Oracle DB ingestor above, but roots them under
# its own Project node (id "plsql://...") instead of the SQL ingestor's Database/
# Schema -- source files carry no Oracle owner/schema. It also creates by-name Table
# nodes as endpoints for READS/WRITES/HAS_TRIGGER (again, no schema to hang them off).
# Distinct from the DB path's oracle:// Table ids, so the two won't merge -- a
# documented limitation (see resolution.py). HAS_PROJECT wraps a Project in an
# optional Solution, matching the C# ingestor's convention.
FILE_NODE_SCHEMAS = [
    NodeSchema(label="Solution", indexed_properties=("name",)),
    NodeSchema(label="Project", indexed_properties=("name",)),
    NodeSchema(label="Package", indexed_properties=("name",)),
    NodeSchema(label="Procedure", indexed_properties=("name",)),
    NodeSchema(label="Function", indexed_properties=("name",)),
    NodeSchema(label="Parameter", indexed_properties=("name",)),
    NodeSchema(label="Trigger", indexed_properties=("name",)),
    NodeSchema(label="Type", indexed_properties=("name",)),
    # SQL DDL entities -- same labels the live `sql` ingestor writes, so a graph
    # viewer/query treats file-ingested and DB-reflected schema the same way.
    NodeSchema(label="Table", indexed_properties=("name",)),
    NodeSchema(label="Column", indexed_properties=("name",)),
    NodeSchema(label="View", indexed_properties=("name",)),
    NodeSchema(label="Index", indexed_properties=("name",)),
    NodeSchema(label="Constraint", indexed_properties=("name",)),
    NodeSchema(label="Sequence", indexed_properties=("name",)),
]

FILE_EDGE_SCHEMAS = [
    EdgeSchema(rel_type="HAS_PROJECT"),
    EdgeSchema(rel_type="HAS_TABLE"),
    EdgeSchema(rel_type="HAS_COLUMN"),
    EdgeSchema(rel_type="HAS_VIEW"),
    EdgeSchema(rel_type="HAS_INDEX"),
    EdgeSchema(rel_type="HAS_CONSTRAINT"),
    EdgeSchema(rel_type="HAS_SEQUENCE"),
    EdgeSchema(rel_type="CONSTRAINS"),
    EdgeSchema(rel_type="INDEXES"),
    EdgeSchema(rel_type="REFERENCES"),
    *EDGE_SCHEMAS,
]
