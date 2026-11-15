"""Graph schema (node/edge labels) the Julia ingestor writes. git_sync passes NODE_SCHEMAS to
setup_schema()/read_fingerprints() so the delta-sync pipeline (core/graph_sync.py) can index
and diff this project's Julia subgraph.

Julia has NO classes -- it is modules over types with multiple-dispatch generic functions -- so
the model differs from the class-based ingestors where the language does:
- The tier is `Module` (HAS_MODULE / HAS_SUBMODULE), like Python's Package tier; items outside
  any `module` block hang off the Project directly (Julia files are not modules).
- Types are `Struct` / `AbstractType` / `PrimitiveType` (no `Class`/`Interface`).
- Callables split into `Function` (the generic function, one per (scope, name)) and `Method`
  (each concrete definition), linked by HAS_METHOD -- Julia's real generic-function/method model.
- `DISPATCHES_ON` (Method -> type) is Julia-specific: the argument types a method dispatches on.

The shared cross-ingestor set is honored: callables are `Function`/`Method`, the call edge is
`CALLS` (carrying a `resolution` provenance), `Parameter`/`HAS_PARAMETER` match, subtyping maps
to `EXTENDS`, and type-reference dependencies use `DEPENDS_ON`."""

from __future__ import annotations

from auryn.core.graph_model import EdgeSchema, NodeSchema

NODE_SCHEMAS = [
    NodeSchema(label="Project", indexed_properties=("name",)),
    NodeSchema(label="Module", indexed_properties=("name",)),
    NodeSchema(label="File", indexed_properties=("path",)),
    NodeSchema(label="Struct", indexed_properties=("name",)),
    NodeSchema(label="AbstractType", indexed_properties=("name",)),
    NodeSchema(label="PrimitiveType", indexed_properties=("name",)),
    NodeSchema(label="Function", indexed_properties=("name", "linkage_key")),  # linkage_key: ADR 0004 cross-repo
    NodeSchema(label="Method", indexed_properties=("name",)),
    NodeSchema(label="Field", indexed_properties=("name",)),
    NodeSchema(label="Parameter", indexed_properties=("name",)),
]

EDGE_SCHEMAS = [
    # Containment backbone -- pure structure, no properties.
    EdgeSchema(rel_type="HAS_MODULE"),     # Project -> Module (top-level module block)
    EdgeSchema(rel_type="HAS_SUBMODULE"),  # Module -> Module (nested module)
    EdgeSchema(rel_type="HAS_FILE"),       # Project -> File
    EdgeSchema(rel_type="HAS_TYPE"),       # Module|Project -> Struct|AbstractType|PrimitiveType
    EdgeSchema(rel_type="HAS_FUNCTION"),   # Module|Project -> Function
    EdgeSchema(rel_type="HAS_METHOD"),     # Function -> Method
    EdgeSchema(rel_type="HAS_FIELD"),      # Struct -> Field
    EdgeSchema(rel_type="HAS_PARAMETER"),  # Method -> Parameter
    EdgeSchema(rel_type="DEFINED_IN"),     # Struct|AbstractType|PrimitiveType|Method -> File
    # Semantic layer.
    EdgeSchema(rel_type="EXTENDS"),        # type -> type (the `<:` subtype relation)
    EdgeSchema(rel_type="DISPATCHES_ON"),  # Method -> type (Julia-specific: the dispatch argument types)
    EdgeSchema(rel_type="CALLS"),          # Method -> Function; {resolution}
    EdgeSchema(rel_type="DEPENDS_ON"),     # Method|Field -> type (param/return/field type refs)
]

# Every label that can be an EXTENDS/DISPATCHES_ON/DEPENDS_ON/HAS_TYPE type endpoint.
TYPE_LABELS = ("Struct", "AbstractType", "PrimitiveType")
