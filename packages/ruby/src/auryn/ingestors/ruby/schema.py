"""Graph schema (node/edge labels) the Ruby ingestor writes. git_sync passes NODE_SCHEMAS to
setup_schema()/read_fingerprints() so the delta-sync pipeline (core/graph_sync.py) can index
and diff this project's Ruby subgraph.

Ruby is class-based (like Python/Java/C#), so the model uses a `Class` tier -- not Rust's
Struct/Trait -- alongside a `Module` tier (Ruby modules are namespaces AND mixins). Callables
split into free `Function`s (a top-level `def`, owned by a Module/Project via HAS_FUNCTION) and
`Method`s (a `def` inside a class/module, bound via HAS_METHOD; `is_singleton` marks a
class/singleton method). Attributes declared by `attr_accessor`/`attr_reader`/`attr_writer` are
`Field`s; uppercase-initial constant assignments are `Const`s.

Ruby semantic edges: EXTENDS carries the superclass (`class Dog < Animal` -> Dog EXTENDS Animal);
INCLUDES carries a mixin (`include`/`prepend`/`extend M` -> Class|Module INCLUDES Module, with a
`kind` property) -- the Ruby analog of Rust's IMPLEMENTS / Go's EMBEDS. IMPORTS carries the
`require`/`require_relative` graph (File -> File) once a require resolves to a project file.

Every cross-entity semantic edge carries a `resolution` provenance ("name" = the fast tree-sitter
engine's name-based match; "external" = a nameable out-of-project placeholder, ADR 0004). A deep
Ruby resolver would re-emit the same ids with a stronger provenance -- the two-phase model,
matching the discipline of the Rust/Go/Python ingestors.

The shared cross-ingestor contract is honoured: callables are `Function`/`Method`, the call edge
is `CALLS` (carrying `resolution`), and `Parameter`/`HAS_PARAMETER`/`EXTENDS`/`DEFINED_IN` match
the other ingestors."""

from __future__ import annotations

from auryn.core.graph_model import EdgeSchema, NodeSchema

NODE_SCHEMAS = [
    NodeSchema(label="Project", indexed_properties=("name",)),
    NodeSchema(label="Module", indexed_properties=("name",)),
    NodeSchema(label="File", indexed_properties=("path",)),
    NodeSchema(label="Class", indexed_properties=("name",)),
    NodeSchema(label="Function", indexed_properties=("name", "linkage_key")),  # linkage_key: ADR 0004 cross-repo
    NodeSchema(label="Method", indexed_properties=("name", "linkage_key")),    # linkage_key: ADR 0004 cross-repo
    NodeSchema(label="Field", indexed_properties=("name",)),
    NodeSchema(label="Parameter", indexed_properties=("name",)),
    NodeSchema(label="Const", indexed_properties=("name",)),
]

EDGE_SCHEMAS = [
    # Containment backbone -- pure structure, no properties (as in Rust/Java/Python).
    EdgeSchema(rel_type="HAS_MODULE"),     # Project|Module|Class -> Module
    EdgeSchema(rel_type="HAS_CLASS"),      # Project|Module|Class -> Class (incl. nested classes)
    EdgeSchema(rel_type="HAS_FILE"),       # Project -> File
    EdgeSchema(rel_type="HAS_FUNCTION"),   # Project|Module -> Function (top-level defs)
    EdgeSchema(rel_type="HAS_METHOD"),     # Class|Module -> Method
    EdgeSchema(rel_type="HAS_FIELD"),      # Class|Module -> Field (attr_accessor/reader/writer)
    EdgeSchema(rel_type="HAS_PARAMETER"),  # Function|Method -> Parameter
    EdgeSchema(rel_type="HAS_CONST"),      # Project|Module|Class -> Const
    EdgeSchema(rel_type="DEFINED_IN"),     # Class|Module -> File
    # Semantic layer.
    EdgeSchema(rel_type="EXTENDS"),   # Class -> Class (superclass); {resolution}
    EdgeSchema(rel_type="INCLUDES"),  # Class|Module -> Module (mixin); {kind, resolution}
    EdgeSchema(rel_type="CALLS"),     # Function|Method -> Function|Method; {resolution, receiver?}
    EdgeSchema(rel_type="IMPORTS"),   # File -> File (require/require_relative); {resolution, kind, target}
]

# Every label that can be an EXTENDS/INCLUDES/HAS_CLASS/HAS_MODULE namespace endpoint.
TYPE_LABELS = ("Class", "Module")
