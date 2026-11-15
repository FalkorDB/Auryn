"""Graph schema (node/edge labels) the Rust ingestor writes. git_sync passes NODE_SCHEMAS to
setup_schema()/read_fingerprints() so the delta-sync pipeline (core/graph_sync.py) can index
and diff this project's Rust subgraph.

Rust is module-based, not class-based, so the model differs from Java/C#: a Cargo tier
(Project -HAS_PACKAGE-> Package -HAS_TARGET-> Crate, issue #5) tops a `Module` tier
(HAS_MODULE/HAS_SUBMODULE, like the Python ingestor's package tier) instead of a package/namespace.
The **crate** (one build target) is the scope root -- so a workspace's distinct crates keep
identically-named items apart -- and callables split into free `Function`s (owned by a Crate/Module
via HAS_FUNCTION) and `Method`s (bound to a Struct/Enum/Trait via HAS_METHOD -- one per fn in an
`impl`/`trait` block, `is_associated` marking a no-`self` associated function). The key Rust
semantic edge is IMPLEMENTS (`impl Trait for Type` -> Type IMPLEMENTS Trait); EXTENDS carries
supertrait bounds (`trait Foo: Bar` -> Foo EXTENDS Bar).

Every cross-entity semantic edge carries a `resolution` provenance ("name" = the fast
tree-sitter engine's name-based match; "resolved" would be a future rust-analyzer deep pass) so
consumers can tell a syntactic guess from a compiler-resolved fact -- the same provenance
discipline the Java/C/Python ingestors use.

The shared cross-ingestor set is held to the contract: callables are `Function`/`Method`, the
call edge is `CALLS` (carrying a `resolution` provenance), and `Parameter`/`HAS_PARAMETER`/
`EXTENDS`/`IMPLEMENTS`/`DEPENDS_ON`/`DEFINED_IN` match the other ingestors."""

from __future__ import annotations

from auryn.core.graph_model import EdgeSchema, NodeSchema

NODE_SCHEMAS = [
    NodeSchema(label="Project", indexed_properties=("name",)),
    # Cargo tier (issue #5): a Package is one Cargo.toml; a Crate is one build target (lib/bin/
    # test/example/bench) -- the crate is the per-file scope root, so multiple crates in a workspace
    # keep identically-named items distinct. Both are aggregation-tier nodes like Go/Java `Package`
    # and Rust `Module`: props are id-derived (manifest facts -- edition/version/deps -- are applied
    # by the git_sync reconcile, which has source access the delta callbacks lack).
    NodeSchema(label="Package", indexed_properties=("name",)),
    NodeSchema(label="Crate", indexed_properties=("name",)),
    NodeSchema(label="Module", indexed_properties=("name",)),
    NodeSchema(label="File", indexed_properties=("path",)),
    NodeSchema(label="Struct", indexed_properties=("name",)),
    NodeSchema(label="Enum", indexed_properties=("name",)),
    NodeSchema(label="Trait", indexed_properties=("name",)),
    NodeSchema(label="Function", indexed_properties=("name", "linkage_key")),  # linkage_key: ADR 0004 cross-repo
    NodeSchema(label="Method", indexed_properties=("name", "linkage_key")),  # linkage_key: ADR 0004 cross-repo
    NodeSchema(label="Field", indexed_properties=("name",)),
    NodeSchema(label="Parameter", indexed_properties=("name",)),
    NodeSchema(label="Const", indexed_properties=("name",)),
    NodeSchema(label="EnumVariant", indexed_properties=("name",)),
]

EDGE_SCHEMAS = [
    # Cargo tier (issue #5) -- the crate backbone above the module tier.
    EdgeSchema(rel_type="HAS_PACKAGE"),    # Project -> Package (one per Cargo.toml)
    EdgeSchema(rel_type="HAS_TARGET"),     # Package -> Crate (lib/bin/test/example/bench)
    # Containment backbone -- pure structure, no properties (as in Java/C#/Python).
    EdgeSchema(rel_type="HAS_MODULE"),     # Crate -> Module (crate-root `mod`)
    EdgeSchema(rel_type="HAS_SUBMODULE"),  # Module -> Module (nested `mod`)
    EdgeSchema(rel_type="HAS_FILE"),       # Project -> File
    EdgeSchema(rel_type="HAS_TYPE"),       # Module|Crate -> Struct|Enum|Trait
    EdgeSchema(rel_type="HAS_FUNCTION"),   # Module|Crate -> Function (free functions)
    EdgeSchema(rel_type="HAS_METHOD"),     # Struct|Enum|Trait -> Method (impl/trait-block fns)
    EdgeSchema(rel_type="HAS_FIELD"),      # Struct -> Field
    EdgeSchema(rel_type="HAS_VARIANT"),    # Enum -> EnumVariant
    EdgeSchema(rel_type="HAS_PARAMETER"),  # Function|Method -> Parameter
    EdgeSchema(rel_type="HAS_CONST"),      # Module|Crate -> Const (module-level const/static)
    EdgeSchema(rel_type="DEFINED_IN"),     # Struct|Enum|Trait -> File
    # Semantic layer.
    EdgeSchema(rel_type="IMPLEMENTS"),  # Struct|Enum -> Trait (impl Trait for Type)
    EdgeSchema(rel_type="EXTENDS"),     # Trait -> Trait (supertrait bounds)
    EdgeSchema(rel_type="CALLS"),       # Function|Method -> Function|Method; {resolution}
    # DEPENDS_ON spans two endpoint shapes (both are "A depends on B"): a callable/field -> a type
    # (param/return/field refs, emitted by resolution) AND a Package -> Package crate dependency
    # (from [dependencies], added by the git_sync manifest reconcile; carries {kind, resolution}).
    EdgeSchema(rel_type="DEPENDS_ON"),
]

# Every label that can be an IMPLEMENTS/EXTENDS/HAS_TYPE/DEPENDS_ON type endpoint.
TYPE_LABELS = ("Struct", "Enum", "Trait")
