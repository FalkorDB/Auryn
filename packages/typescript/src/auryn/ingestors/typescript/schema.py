"""Graph schema (node/edge labels) the TypeScript/JavaScript ingestor writes. git_sync passes
NODE_SCHEMAS to setup_schema()/read_fingerprints() so the delta-sync pipeline
(core/graph_sync.py) can index and diff this project's JS/TS subgraph.

Modelled closest to the Python ingestor, because the languages line up: a JS/TS file *is* a
module (ES modules), so `Module` (per file) replaces C#'s File/Namespace split, and `Function`
(module-level) exists alongside `Method` (class/interface member) because JS/TS has free
functions and C# doesn't. TS-only concepts add their own labels (`Interface`, `Enum`,
`TypeAlias`) and the module import graph adds `IMPORTS` -- all additive, never renaming the
shared vocabulary.

The shared cross-ingestor set is held to the contract: callables are `Function`/`Method`, the
call edge is `CALLS`, and `Parameter`/`HAS_PARAMETER`/`EXTENDS`/`IMPLEMENTS` match the other
ingestors. Types are optional (absent in JS, erasable in TS), so every type-bearing edge
carries a `resolution` provenance ("name" = the fast tree-sitter name-based match; a future
deep tsserver pass would emit "resolved") and Module carries a `dialect` ("ts"/"js") marker --
the same provenance discipline the Python/C ingestors use."""

from __future__ import annotations

from auryn.core.graph_model import EdgeSchema, NodeSchema

NODE_SCHEMAS = [
    NodeSchema(label="Project", indexed_properties=("name",)),
    # A module == a file. Carries dialect ("ts"/"js") and is_declaration (.d.ts).
    NodeSchema(label="Module", indexed_properties=("path",)),
    NodeSchema(label="Class", indexed_properties=("name", "linkage_key")),  # linkage_key: ADR 0004
    NodeSchema(label="Interface", indexed_properties=("name", "linkage_key")),
    NodeSchema(label="Enum", indexed_properties=("name",)),
    NodeSchema(label="TypeAlias", indexed_properties=("name",)),
    NodeSchema(label="Function", indexed_properties=("name", "linkage_key")),  # module-level
    NodeSchema(label="Method", indexed_properties=("name", "linkage_key")),  # class/interface member
    NodeSchema(label="Property", indexed_properties=("name",)),  # class/interface member
    NodeSchema(label="Parameter", indexed_properties=("name",)),
    NodeSchema(label="EnumMember", indexed_properties=("name",)),
    # HTTP call target (ADR 0006): a route-keyed endpoint a caller reaches over HTTP. Emitted
    # here as a resolved:false placeholder (shared extern://http/ id) that the HTTP stitch hook
    # resolves to the handler across repos. Indexed on route so the hook's match is a seek.
    NodeSchema(label="APIEndpoint", indexed_properties=("route",)),
]

EDGE_SCHEMAS = [
    # Containment backbone -- pure structure, no properties (as in Python/C#).
    EdgeSchema(rel_type="HAS_MODULE"),  # Project -> Module
    EdgeSchema(rel_type="HAS_CLASS"),  # Module -> Class
    EdgeSchema(rel_type="HAS_INTERFACE"),  # Module -> Interface
    EdgeSchema(rel_type="HAS_ENUM"),  # Module -> Enum
    EdgeSchema(rel_type="HAS_TYPE_ALIAS"),  # Module -> TypeAlias
    EdgeSchema(rel_type="HAS_FUNCTION"),  # Module -> Function
    EdgeSchema(rel_type="HAS_METHOD"),  # Class|Interface -> Method
    EdgeSchema(rel_type="HAS_PROPERTY"),  # Class|Interface -> Property
    EdgeSchema(rel_type="HAS_PARAMETER"),  # Function|Method -> Parameter
    EdgeSchema(rel_type="HAS_ENUM_MEMBER"),  # Enum -> EnumMember
    # Semantic layer -- each carries a `resolution` provenance (see module docstring).
    EdgeSchema(rel_type="IMPORTS"),  # Module -> Module (import specifier resolved in-project)
    EdgeSchema(rel_type="EXTENDS"),  # Class -> Class ; Interface -> Interface (multi)
    EdgeSchema(rel_type="IMPLEMENTS"),  # Class -> Interface
    EdgeSchema(rel_type="CALLS"),  # Function|Method -> Function|Method; {resolution, receiver}
    EdgeSchema(rel_type="RETURNS"),  # Function|Method -> Class|Interface (local return type)
    EdgeSchema(rel_type="REFERENCES"),  # Parameter|Property|Function|Method -> Class|Interface|Enum|TypeAlias
    EdgeSchema(rel_type="CALLS_API"),  # Function|Method -> APIEndpoint (ADR 0006; {resolution, provenance})
]

# Every label that can be an EXTENDS/IMPLEMENTS/HAS_*/REFERENCES type endpoint -- i.e. every
# "type declaration" kind. Mirrors the C#/Python TYPE_LABELS scans.
TYPE_LABELS = ("Class", "Interface", "Enum", "TypeAlias")
