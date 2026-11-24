"""Graph schema (node/edge labels) the C ingestor writes. git_sync passes NODE_SCHEMAS to
setup_schema()/read_fingerprints() so the delta-sync pipeline (core/graph_sync.py) can index
and diff this project's C subgraph.

C has no classes/methods, so the model is flatter than C#/Python: a File contains free
functions and type declarations; there's no namespace tier. Linkage, not lexical nesting,
decides identity (see ids.py). Every cross-entity semantic edge carries a `resolution`
provenance ("name" = the fast tree-sitter engine's name-based match; "resolved" would be a
future libclang deep pass) so consumers can tell a syntactic guess from a compiler-resolved
fact -- the same provenance discipline the Python ingestor uses."""

from __future__ import annotations

from auryn.core.graph_model import EdgeSchema, NodeSchema

NODE_SCHEMAS = [
    NodeSchema(label="Project", indexed_properties=("name",)),
    NodeSchema(label="File", indexed_properties=("path",)),
    NodeSchema(label="Function", indexed_properties=("name", "linkage_key")),  # linkage_key: ADR 0004 cross-repo
    NodeSchema(label="Struct", indexed_properties=("name",)),
    NodeSchema(label="Union", indexed_properties=("name",)),
    NodeSchema(label="Enum", indexed_properties=("name",)),
    NodeSchema(label="TypeAlias", indexed_properties=("name",)),
    NodeSchema(label="GlobalVariable", indexed_properties=("name",)),
    NodeSchema(label="Macro", indexed_properties=("name",)),
    NodeSchema(label="Field", indexed_properties=("name",)),
    NodeSchema(label="Parameter", indexed_properties=("name",)),
]

EDGE_SCHEMAS = [
    # File <-> entity: a file DEFINES an entity (has its body/definition) or DECLARES it (a
    # prototype / forward declaration). A single external function is typically DECLARED by
    # one or more headers and DEFINED by exactly one .c -- so one Function node, many edges.
    EdgeSchema(rel_type="DEFINES"),   # File -> Function|Struct|Union|Enum|TypeAlias|GlobalVariable|Macro
    EdgeSchema(rel_type="DECLARES"),  # File -> Function|Struct (prototype / forward decl)
    EdgeSchema(rel_type="INCLUDES"),  # File -> File (#include resolved within the project)
    # Structural members.
    EdgeSchema(rel_type="HAS_FIELD"),      # Struct|Union -> Field
    EdgeSchema(rel_type="HAS_PARAMETER"),  # Function -> Parameter
    # Semantic layer -- each carries {resolution}.
    EdgeSchema(rel_type="CALLS"),       # Function -> Function
    EdgeSchema(rel_type="REFERENCES"),  # Function|Field -> Struct|Union|Enum|TypeAlias (type usage)
]

# Named-type labels a REFERENCES edge can point at (a field/param/return type naming one).
TYPE_LABELS = ("Struct", "Union", "Enum", "TypeAlias")
