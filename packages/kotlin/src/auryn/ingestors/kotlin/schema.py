"""Graph schema (node/edge labels) the Kotlin ingestor writes. git_sync passes NODE_SCHEMAS to
setup_schema()/read_fingerprints() so the delta-sync pipeline (core/graph_sync.py) can index and
diff this project's Kotlin subgraph.

Mirrors ingestors/java/schema.py -- Kotlin and Java are JVM siblings (a package tier over types
with methods/fields). Kotlin differences track the language: it has top-level callables and
properties, so there is a `Function` label + HAS_FUNCTION edge (as in the Rust ingestor) beside
`Method`/HAS_METHOD, and a Field may hang off a Package/Project (a top-level `val`/`var`) as well
as a type; there is no Record label (Kotlin `data class` -> Class with `is_data`, `object` ->
Class with `is_object`). A deep Kotlin-compiler / kotlin-language-server pass is the future analog
of Java's JDT/javac deep phase.

The shared cross-ingestor set is held to the contract: callables are `Function`/`Method`, the
call edge is `CALLS` (carrying a `resolution` provenance), and `Parameter`/`HAS_PARAMETER`/
`EXTENDS`/`IMPLEMENTS`/`DEPENDS_ON`/`DEFINED_IN` match the other ingestors."""

from __future__ import annotations

from auryn.core.graph_model import EdgeSchema, NodeSchema

NODE_SCHEMAS = [
    NodeSchema(label="Project", indexed_properties=("name",)),
    NodeSchema(label="Module", indexed_properties=("name",)),  # Maven/Gradle subproject (scope root; no linkage_key -- see kotlinmod)
    NodeSchema(label="Package", indexed_properties=("name",)),
    NodeSchema(label="File", indexed_properties=("path",)),
    NodeSchema(label="Class", indexed_properties=("name",)),
    NodeSchema(label="Interface", indexed_properties=("name",)),
    NodeSchema(label="Enum", indexed_properties=("name",)),
    NodeSchema(label="Function", indexed_properties=("name", "linkage_key")),  # linkage_key: ADR 0004 cross-repo
    NodeSchema(label="Method", indexed_properties=("name", "linkage_key")),  # linkage_key: ADR 0004 cross-repo
    NodeSchema(label="Field", indexed_properties=("name",)),
    NodeSchema(label="Parameter", indexed_properties=("name",)),
    NodeSchema(label="EnumMember", indexed_properties=("name",)),
]

EDGE_SCHEMAS = [
    # Containment backbone -- pure structure, no properties (as in Java/Rust/Python).
    EdgeSchema(rel_type="HAS_MODULE"),       # Project -> Module (one per Maven/Gradle subproject)
    EdgeSchema(rel_type="HAS_PACKAGE"),      # Module|Project -> Package (first segment; Project for root files)
    EdgeSchema(rel_type="HAS_SUBPACKAGE"),   # Package -> Package
    EdgeSchema(rel_type="HAS_FILE"),         # Project -> File
    EdgeSchema(rel_type="HAS_TYPE"),         # Package|Module|Project -> Class|Interface|Enum
    EdgeSchema(rel_type="HAS_NESTED_TYPE"),  # type -> type
    EdgeSchema(rel_type="DEFINED_IN"),       # type -> File
    EdgeSchema(rel_type="HAS_METHOD"),       # type -> Method (member funs + constructors)
    EdgeSchema(rel_type="HAS_FUNCTION"),     # Package|Module|Project -> Function (top-level funs)
    EdgeSchema(rel_type="HAS_FIELD"),        # type|Package|Module|Project -> Field (properties)
    EdgeSchema(rel_type="HAS_PARAMETER"),    # Method|Function -> Parameter
    EdgeSchema(rel_type="HAS_ENUM_MEMBER"),  # Enum -> EnumMember
    # Semantic layer.
    EdgeSchema(rel_type="EXTENDS"),      # Class -> Class ; Interface -> Interface(s)
    EdgeSchema(rel_type="IMPLEMENTS"),   # Class|Enum|Object -> Interface
    EdgeSchema(rel_type="CALLS"),        # Function|Method -> Function|Method; {resolution, receiver_type?}
    EdgeSchema(rel_type="DEPENDS_ON"),   # Method|Function|Field -> type (parameter/return/field type refs)
]

# Every label that can be an EXTENDS/IMPLEMENTS/HAS_TYPE/DEPENDS_ON type endpoint.
TYPE_LABELS = ("Class", "Interface", "Enum")
