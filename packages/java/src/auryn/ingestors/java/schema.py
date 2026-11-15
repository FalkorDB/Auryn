"""Graph schema (node/edge labels) the Java ingestor writes. git_sync passes NODE_SCHEMAS to
setup_schema()/read_fingerprints() so the delta-sync pipeline (core/graph_sync.py) can index
and diff this project's Java subgraph.

Mirrors ingestors/csharp/schema.py -- Java and C# are structural siblings (a namespace/package
tier over types with methods/fields; no free functions). Differences track the language: the
tier is `Package` (with HAS_PACKAGE/HAS_SUBPACKAGE, as in the Python ingestor) rather than C#'s
`Namespace`; there are no Property/Event/Delegate/Struct labels (Java has none); and the C#
DTO/APIEndpoint classification layer is deferred (a Spring `@RestController` enrichment is the
natural future analog).

The shared cross-ingestor set is held to the contract: callables are `Method`, the call edge is
`CALLS` (carrying a `resolution` provenance), and `Parameter`/`HAS_PARAMETER`/`EXTENDS`/
`IMPLEMENTS` match the other ingestors."""

from __future__ import annotations

from auryn.core.graph_model import EdgeSchema, NodeSchema

NODE_SCHEMAS = [
    NodeSchema(label="Project", indexed_properties=("name",)),
    NodeSchema(label="Module", indexed_properties=("name",)),  # Maven/Gradle subproject (scope root; no linkage_key -- see javamod)
    NodeSchema(label="Package", indexed_properties=("name",)),
    NodeSchema(label="File", indexed_properties=("path",)),
    NodeSchema(label="Class", indexed_properties=("name",)),
    NodeSchema(label="Interface", indexed_properties=("name",)),
    NodeSchema(label="Enum", indexed_properties=("name",)),
    NodeSchema(label="Record", indexed_properties=("name",)),
    NodeSchema(label="Method", indexed_properties=("name", "linkage_key")),  # linkage_key: ADR 0004 cross-repo
    NodeSchema(label="Field", indexed_properties=("name",)),
    NodeSchema(label="Parameter", indexed_properties=("name",)),
    NodeSchema(label="EnumMember", indexed_properties=("name",)),
]

EDGE_SCHEMAS = [
    # Containment backbone -- pure structure, no properties (as in C#/Python).
    EdgeSchema(rel_type="HAS_MODULE"),       # Project -> Module (one per Maven/Gradle subproject)
    EdgeSchema(rel_type="HAS_PACKAGE"),      # Module|Project -> Package (first segment; Project for root files)
    EdgeSchema(rel_type="HAS_SUBPACKAGE"),   # Package -> Package
    EdgeSchema(rel_type="HAS_FILE"),         # Project -> File
    EdgeSchema(rel_type="HAS_TYPE"),         # Package|Module|Project -> Class|Interface|Enum|Record
    EdgeSchema(rel_type="HAS_NESTED_TYPE"),  # type -> type
    EdgeSchema(rel_type="DEFINED_IN"),       # type -> File
    EdgeSchema(rel_type="HAS_METHOD"),       # type -> Method
    EdgeSchema(rel_type="HAS_FIELD"),        # type -> Field
    EdgeSchema(rel_type="HAS_PARAMETER"),    # Method|Record -> Parameter
    EdgeSchema(rel_type="HAS_ENUM_MEMBER"),  # Enum -> EnumMember
    # Semantic layer.
    EdgeSchema(rel_type="EXTENDS"),      # Class -> Class ; Interface -> Interface(s)
    EdgeSchema(rel_type="IMPLEMENTS"),   # Class|Enum|Record -> Interface
    EdgeSchema(rel_type="CALLS"),        # Method -> Method; {resolution, receiver_type?}
    EdgeSchema(rel_type="DEPENDS_ON"),   # Method|Field -> type (parameter/return/field type refs)
]

# Every label that can be an EXTENDS/IMPLEMENTS/HAS_TYPE/DEPENDS_ON type endpoint.
TYPE_LABELS = ("Class", "Interface", "Enum", "Record")
