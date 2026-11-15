"""Graph schema (node/edge labels) the Swift ingestor writes. git_sync passes NODE_SCHEMAS to
setup_schema()/read_fingerprints() so the delta-sync pipeline (core/graph_sync.py) can index
and diff this project's Swift subgraph.

Swift is a hybrid of the Rust and Java/TypeScript models, and the schema reflects both:

* like Rust, Swift has free top-level `Function`s (HAS_FUNCTION off Project) alongside type-bound
  `Method`s, and its members can be declared away from the type in an `extension` block -- so a
  Method binds to its resolved owning type by name (an extension may sit in another file), exactly
  as Rust attaches an `impl` block's methods.
* like Java/TypeScript, the type kinds are nominal (`Class`/`Struct`/`Enum`/`Protocol`) with a
  proper hierarchy: a class superclass -> EXTENDS, protocol conformance -> IMPLEMENTS, and a
  protocol's inherited protocols -> EXTENDS. Members are `Property` (stored + computed) and
  `Method` (incl. initializers), enum cases are `EnumMember` -- the same labels TypeScript uses.

A Swift module is a SwiftPM TARGET -- one directory of sources (`Sources/<Target>/`,
`Tests/<Target>/`) -- and is the compilation + namespace unit (`A.Foo` and `B.Foo` are distinct
types). So a `Module` tier (Project -HAS_MODULE-> Module, path-derived per `swiftpm.py`) sits over
the top-level types/functions, keeping two targets' identically-named symbols apart. A module node
carries a `linkage_key`, so an `import <Target>` in another repo (or a sibling target) heals onto it
(ADR 0004). `import` statements that DON'T resolve to an ingested target still surface as File
-IMPORTS-> Module placeholders (an external framework/package), the same label.

The shared cross-ingestor set is held to the contract: callables are `Function`/`Method`, the
call edge is `CALLS` (carrying a `resolution` provenance), and `Parameter`/`HAS_PARAMETER`/
`EXTENDS`/`IMPLEMENTS`/`DEPENDS_ON`/`DEFINED_IN`/`EnumMember`/`HAS_ENUM_MEMBER` match the other
ingestors. Every semantic edge carries a `resolution` provenance ("name" = the fast tree-sitter
engine's name-based match; "resolved" would be a future SourceKit-LSP deep pass; "external" =
an ADR 0004 out-of-project placeholder) -- the same provenance discipline the Rust/Java/Python
ingestors use."""

from __future__ import annotations

from auryn.core.graph_model import EdgeSchema, NodeSchema

NODE_SCHEMAS = [
    NodeSchema(label="Project", indexed_properties=("name",)),
    NodeSchema(label="File", indexed_properties=("path",)),
    NodeSchema(label="Module", indexed_properties=("name", "linkage_key")),  # imported external modules (ADR 0004)
    NodeSchema(label="Class", indexed_properties=("name",)),
    NodeSchema(label="Struct", indexed_properties=("name",)),
    NodeSchema(label="Enum", indexed_properties=("name",)),
    NodeSchema(label="Protocol", indexed_properties=("name",)),
    NodeSchema(label="Function", indexed_properties=("name", "linkage_key")),  # linkage_key: ADR 0004 cross-repo
    NodeSchema(label="Method", indexed_properties=("name", "linkage_key")),  # linkage_key: ADR 0004 cross-repo
    NodeSchema(label="Property", indexed_properties=("name",)),
    NodeSchema(label="Parameter", indexed_properties=("name",)),
    NodeSchema(label="EnumMember", indexed_properties=("name",)),
]

EDGE_SCHEMAS = [
    # Containment backbone -- pure structure, no properties (as in Rust/Java/TypeScript).
    EdgeSchema(rel_type="HAS_FILE"),         # Project -> File
    EdgeSchema(rel_type="HAS_MODULE"),       # Project -> Module (one per SwiftPM target)
    EdgeSchema(rel_type="HAS_TYPE"),         # Module -> Class|Struct|Enum|Protocol (top-level)
    EdgeSchema(rel_type="HAS_NESTED_TYPE"),  # type -> type (nested)
    EdgeSchema(rel_type="HAS_FUNCTION"),     # Module -> Function (free top-level funcs)
    EdgeSchema(rel_type="HAS_METHOD"),       # type -> Method (own body + extension members + init)
    EdgeSchema(rel_type="HAS_PROPERTY"),     # type -> Property (stored + computed)
    EdgeSchema(rel_type="HAS_PARAMETER"),    # Function|Method -> Parameter
    EdgeSchema(rel_type="HAS_ENUM_MEMBER"),  # Enum -> EnumMember (cases)
    EdgeSchema(rel_type="DEFINED_IN"),       # Class|Struct|Enum|Protocol -> File
    # Semantic layer.
    EdgeSchema(rel_type="IMPORTS"),      # File -> Module (imported module); {resolution}
    EdgeSchema(rel_type="EXTENDS"),      # Class -> Class (superclass) ; Protocol -> Protocol (inherited)
    EdgeSchema(rel_type="IMPLEMENTS"),   # Class|Struct|Enum -> Protocol (conformance, incl. via extension)
    EdgeSchema(rel_type="CALLS"),        # Function|Method -> Function|Method; {resolution, receiver_type?}
    EdgeSchema(rel_type="DEPENDS_ON"),   # Function|Method|Property -> type (param/return/property refs)
]

# Every label that can be an EXTENDS/IMPLEMENTS/HAS_TYPE/DEPENDS_ON type endpoint.
TYPE_LABELS = ("Class", "Struct", "Enum", "Protocol")
