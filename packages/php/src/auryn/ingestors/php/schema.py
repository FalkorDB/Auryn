"""Graph schema (node/edge labels) the PHP ingestor writes. git_sync passes NODE_SCHEMAS to
setup_schema()/read_fingerprints() so the delta-sync pipeline (core/graph_sync.py) can index and
diff this project's PHP subgraph.

Mirrors ingestors/java/schema.py -- PHP and Java are structural siblings (a namespace/package tier
over types with methods; explicit extends/implements). Differences track the language: the tier is
`Namespace` (with HAS_NAMESPACE/HAS_SUBNAMESPACE, like Java's Package tier but backslash-separated);
PHP adds a `Trait` type kind and the `USES_TRAIT` edge, `Property`/`Constant` class members
(HAS_PROPERTY/HAS_CONSTANT), `EnumCase`s (HAS_ENUM_CASE), and top-level free `Function`s
(HAS_FUNCTION) alongside methods.

The shared cross-ingestor set is held to the contract: callables are `Function`/`Method`, the call
edge is `CALLS` (carrying a `resolution` provenance), and `Parameter`/`HAS_PARAMETER`/`EXTENDS`/
`IMPLEMENTS`/`DEPENDS_ON` match the other ingestors."""

from __future__ import annotations

from auryn.core.graph_model import EdgeSchema, NodeSchema

NODE_SCHEMAS = [
    NodeSchema(label="Project", indexed_properties=("name",)),
    NodeSchema(label="Namespace", indexed_properties=("name",)),
    NodeSchema(label="File", indexed_properties=("path",)),
    NodeSchema(label="Class", indexed_properties=("name",)),
    NodeSchema(label="Interface", indexed_properties=("name",)),
    NodeSchema(label="Trait", indexed_properties=("name",)),
    NodeSchema(label="Enum", indexed_properties=("name",)),
    NodeSchema(label="Function", indexed_properties=("name", "linkage_key")),  # linkage_key: ADR 0004 cross-repo
    NodeSchema(label="Method", indexed_properties=("name", "linkage_key")),    # linkage_key: ADR 0004 cross-repo
    NodeSchema(label="Property", indexed_properties=("name",)),
    NodeSchema(label="Constant", indexed_properties=("name",)),
    NodeSchema(label="Parameter", indexed_properties=("name",)),
    NodeSchema(label="EnumCase", indexed_properties=("name",)),
]

EDGE_SCHEMAS = [
    # Containment backbone -- pure structure, no properties (as in Java/C#/Python).
    EdgeSchema(rel_type="HAS_NAMESPACE"),      # Project -> Namespace (first segment)
    EdgeSchema(rel_type="HAS_SUBNAMESPACE"),   # Namespace -> Namespace
    EdgeSchema(rel_type="HAS_FILE"),           # Project -> File
    EdgeSchema(rel_type="HAS_TYPE"),           # Namespace|Project -> Class|Interface|Trait|Enum
    EdgeSchema(rel_type="HAS_FUNCTION"),       # Namespace|Project -> Function
    EdgeSchema(rel_type="DEFINED_IN"),         # type|Function -> File
    EdgeSchema(rel_type="HAS_METHOD"),         # type -> Method
    EdgeSchema(rel_type="HAS_PROPERTY"),       # type -> Property
    EdgeSchema(rel_type="HAS_CONSTANT"),       # type -> Constant
    EdgeSchema(rel_type="HAS_PARAMETER"),      # Method|Function -> Parameter
    EdgeSchema(rel_type="HAS_ENUM_CASE"),      # Enum -> EnumCase
    # Semantic layer.
    EdgeSchema(rel_type="EXTENDS"),      # Class -> Class ; Interface -> Interface(s)
    EdgeSchema(rel_type="IMPLEMENTS"),   # Class|Enum -> Interface
    EdgeSchema(rel_type="USES_TRAIT"),   # Class|Trait|Enum -> Trait  (PHP-specific)
    EdgeSchema(rel_type="CALLS"),        # Method|Function -> Method|Function; {resolution, receiver_type?}
    EdgeSchema(rel_type="DEPENDS_ON"),   # Method|Function|Property -> type (param/return/property refs)
]

# Every label that can be an EXTENDS/IMPLEMENTS/USES_TRAIT/HAS_TYPE/DEPENDS_ON type endpoint.
TYPE_LABELS = ("Class", "Interface", "Trait", "Enum")
