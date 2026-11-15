"""Graph schema (node/edge labels) the Go ingestor writes. git_sync passes NODE_SCHEMAS to
setup_schema()/read_fingerprints() so the delta-sync pipeline (core/graph_sync.py) can index
and diff this project's Go subgraph.

Go is package-based, not class-based, so the model is deliberately NOT a Java class model: a
flat `Package` tier holds free `Function`s and named types (`Struct`/`Interface`/`TypeAlias`),
and a `Method` is a func WITH a receiver, attached to its receiver type (HAS_METHOD). The
distinctive Go edge is `EMBEDS` (struct/interface embedding = composition).

The shared cross-ingestor contract is honoured: callables are `Function` (free) and `Method`
(bound), the call edge is `CALLS` (carrying a `resolution` provenance), and `Parameter`/
`HAS_PARAMETER`/`DEPENDS_ON` match the other ingestors.

**IMPLEMENTS is intentionally absent from this fast pass.** Go interface satisfaction is
structural/implicit and cannot be decided without real type info; it is deferred to a future
deep (go/types or gopls) pass, exactly as Java defers its JDT pass (see plugin.py/resolution.py)."""

from __future__ import annotations

from auryn.core.graph_model import EdgeSchema, NodeSchema

NODE_SCHEMAS = [
    NodeSchema(label="Project", indexed_properties=("name",)),
    # A `Module` is one go.mod; a `Package` is one directory (keyed by dir, not the `package` clause
    # name, so a workspace's many identically-named packages stay distinct). Both are aggregation
    # nodes with id-derived props (go version + require deps are layered on by git_sync's reconcile).
    NodeSchema(label="Module", indexed_properties=("name",)),
    NodeSchema(label="Package", indexed_properties=("name", "import_path")),
    NodeSchema(label="File", indexed_properties=("path",)),
    NodeSchema(label="Struct", indexed_properties=("name",)),
    NodeSchema(label="Interface", indexed_properties=("name",)),
    NodeSchema(label="TypeAlias", indexed_properties=("name",)),
    NodeSchema(label="Function", indexed_properties=("name", "linkage_key")),  # linkage_key: ADR 0004 cross-repo
    NodeSchema(label="Method", indexed_properties=("name", "linkage_key")),    # linkage_key: ADR 0004 cross-repo
    NodeSchema(label="Field", indexed_properties=("name",)),
    NodeSchema(label="Parameter", indexed_properties=("name",)),
    NodeSchema(label="Const", indexed_properties=("name",)),
]

EDGE_SCHEMAS = [
    # Containment backbone -- pure structure, no properties (as in Java/C#/Python).
    EdgeSchema(rel_type="HAS_MODULE"),     # Project -> Module (one per go.mod)
    EdgeSchema(rel_type="HAS_PACKAGE"),    # Module -> Package (or Project -> Package when no go.mod)
    EdgeSchema(rel_type="HAS_FILE"),       # Project -> File
    EdgeSchema(rel_type="HAS_TYPE"),       # Package -> Struct|Interface|TypeAlias
    EdgeSchema(rel_type="HAS_FUNCTION"),   # Package -> Function (free/top-level func)
    EdgeSchema(rel_type="HAS_METHOD"),     # Struct|TypeAlias -> Method ; Interface -> Method (spec)
    EdgeSchema(rel_type="HAS_FIELD"),      # Struct -> Field
    EdgeSchema(rel_type="HAS_PARAMETER"),  # Function|Method -> Parameter
    EdgeSchema(rel_type="HAS_CONST"),      # Package -> Const
    EdgeSchema(rel_type="DEFINED_IN"),     # Struct|Interface|TypeAlias -> File
    # Semantic layer.
    EdgeSchema(rel_type="EMBEDS"),      # Struct -> Struct|Interface ; Interface -> Interface (Go embedding)
    EdgeSchema(rel_type="CALLS"),       # Function|Method -> Function|Method; {resolution}
    # DEPENDS_ON spans two endpoint shapes (both "A depends on B"): a callable/field -> a type
    # (param/return/field refs, from resolution) AND a Module -> Module require (from go.mod, added
    # by the git_sync reconcile; carries {kind, resolution}).
    EdgeSchema(rel_type="DEPENDS_ON"),
    # NOTE: no IMPLEMENTS -- structural interface satisfaction is deferred to the deep pass.
]

# Every label that can be an EMBEDS/HAS_TYPE/DEPENDS_ON type endpoint.
TYPE_LABELS = ("Struct", "Interface", "TypeAlias")
