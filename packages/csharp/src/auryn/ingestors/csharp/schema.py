"""Graph schema (node/edge labels) the C# ingestor writes."""

from __future__ import annotations

from auryn.core.graph_model import EdgeSchema, NodeSchema

NODE_SCHEMAS = [
    NodeSchema(label="Solution", indexed_properties=("name",)),
    NodeSchema(label="Project", indexed_properties=("name",)),
    NodeSchema(label="Namespace", indexed_properties=("name",)),
    NodeSchema(label="File", indexed_properties=("name",)),
    NodeSchema(label="Class", indexed_properties=("name",)),
    NodeSchema(label="Interface", indexed_properties=("name",)),
    NodeSchema(label="Struct", indexed_properties=("name",)),
    NodeSchema(label="Enum", indexed_properties=("name",)),
    NodeSchema(label="Record", indexed_properties=("name",)),
    NodeSchema(label="Delegate", indexed_properties=("name",)),
    NodeSchema(label="DTO", indexed_properties=("name",)),
    NodeSchema(label="Method", indexed_properties=("name", "linkage_key")),  # linkage_key: ADR 0004 cross-repo
    NodeSchema(label="Property", indexed_properties=("name",)),
    NodeSchema(label="Field", indexed_properties=("name",)),
    NodeSchema(label="Event", indexed_properties=("name",)),
    NodeSchema(label="Parameter", indexed_properties=("name",)),
    NodeSchema(label="EnumMember", indexed_properties=("name",)),
    NodeSchema(label="APIEndpoint", indexed_properties=("route",)),
    # Written only by the Roslyn engine: one node per DI registration site (see
    # ingestors/csharp/roslyn.py). The tree-sitter engine can't resolve DI.
    NodeSchema(label="DIRegistration", indexed_properties=("service_type",)),
    # Kendo/ASP.NET MVC view tier (see ingestors/csharp/kendo.py), ingested as a
    # stage of C# git-sync so its seam edges land on the C# Method/APIEndpoint nodes.
    NodeSchema(label="View", indexed_properties=("name",)),
    NodeSchema(label="Grid", indexed_properties=("name",)),
    NodeSchema(label="Field", indexed_properties=("name",)),
    NodeSchema(label="EventHandler", indexed_properties=("name",)),
]

EDGE_SCHEMAS = [
    EdgeSchema(rel_type="HAS_PROJECT"),
    EdgeSchema(rel_type="HAS_NAMESPACE"),
    EdgeSchema(rel_type="HAS_FILE"),
    EdgeSchema(rel_type="HAS_TYPE"),
    EdgeSchema(rel_type="HAS_NESTED_TYPE"),
    EdgeSchema(rel_type="DEFINED_IN"),
    EdgeSchema(rel_type="HAS_METHOD"),
    EdgeSchema(rel_type="HAS_PROPERTY"),
    EdgeSchema(rel_type="HAS_FIELD"),
    EdgeSchema(rel_type="HAS_EVENT"),
    EdgeSchema(rel_type="HAS_ENUM_MEMBER"),
    EdgeSchema(rel_type="HAS_PARAMETER"),
    EdgeSchema(rel_type="EXTENDS"),
    EdgeSchema(rel_type="IMPLEMENTS"),
    EdgeSchema(rel_type="CALLS"),
    EdgeSchema(rel_type="DEPENDS_ON"),
    EdgeSchema(rel_type="EXPOSES_ENDPOINT"),
    EdgeSchema(rel_type="ACCEPTS"),
    EdgeSchema(rel_type="RETURNS"),
    # Roslyn engine only: DI registration edges (DIRegistration->service/impl) and
    # the resolved interface->implementation edge.
    EdgeSchema(rel_type="REGISTERS"),
    EdgeSchema(rel_type="RESOLVES_TO"),
    # Kendo/ASP.NET MVC view tier (kendo.py). CALLS_API/SUBMITS_TO/DISPLAYS_DATA are
    # the golden-path seam edges from the UI onto C# Method/APIEndpoint nodes.
    EdgeSchema(rel_type="CONTAINS"),
    EdgeSchema(rel_type="HANDLES"),
    EdgeSchema(rel_type="CALLS_API"),
    EdgeSchema(rel_type="SUBMITS_TO"),
    EdgeSchema(rel_type="DISPLAYS_DATA"),
]

# Every label that can appear as the target of HAS_TYPE / HAS_NESTED_TYPE / EXTENDS's
# other side / IMPLEMENTS -- i.e. every "type declaration" kind, including the
# classification-only DTO label.
TYPE_LABELS = (
    "Class",
    "Interface",
    "Struct",
    "Enum",
    "Record",
    "Delegate",
    "DTO",
)
