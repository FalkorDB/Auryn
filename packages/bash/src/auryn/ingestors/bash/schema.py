"""Graph schema (node/edge labels) the Bash ingestor writes. git_sync passes NODE_SCHEMAS to
setup_schema()/read_fingerprints() so the delta-sync pipeline (core/graph_sync.py) can index
and diff this project's Bash subgraph.

Bash has NO packages, NO classes and NO types, so the model is deliberately flat: a `File` owns
the `Function`s it defines (HAS_FUNCTION), a function CALLS the local functions and external
commands it invokes, and a file IMPORTS the files it `source`s. There is no Package/Struct/
Interface/Method/Parameter tier -- the language has no such concepts, and the fast pass invents
nothing.

The shared cross-ingestor contract is honoured where it maps: callables are `Function`, the call
edge is `CALLS` (carrying a `resolution` provenance), `File`/`HAS_FILE` match the other ingestors,
and `IMPORTS` (used here for `source`/`.`) is the same edge type the Python/PHP/... ingestors use.
`linkage_key` is indexed on `Function` for ADR 0004 cross-repo reconciliation."""

from __future__ import annotations

from auryn.core.graph_model import EdgeSchema, NodeSchema

NODE_SCHEMAS = [
    NodeSchema(label="Project", indexed_properties=("name",)),
    NodeSchema(label="File", indexed_properties=("path",)),
    NodeSchema(label="Function", indexed_properties=("name", "linkage_key")),  # linkage_key: ADR 0004 cross-repo
]

EDGE_SCHEMAS = [
    # Containment backbone -- pure structure, no properties (as in the other ingestors).
    EdgeSchema(rel_type="HAS_FILE"),      # Project -> File
    EdgeSchema(rel_type="HAS_FUNCTION"),  # File -> Function (bash has no package/class tier)
    # Semantic layer.
    EdgeSchema(rel_type="CALLS"),    # Function -> Function (local func or external command); {resolution}
    EdgeSchema(rel_type="IMPORTS"),  # File -> File (`source`/`.` include); {resolution}
    # NOTE: no Package/Struct/Interface/Method/Parameter and no IMPLEMENTS/EXTENDS/DEPENDS_ON --
    # Bash has no modules, types, classes or typed parameters, so those tiers/edges do not exist.
]
