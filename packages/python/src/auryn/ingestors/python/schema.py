"""Graph schema (node/edge labels) the Python ingestor writes.

Mirrors ingestors/csharp/schema.py: both Python engines (`jedi` and `ast`) emit only
these labels/edges, and git_sync passes this list to setup_schema()/read_fingerprints()
so the delta-sync pipeline (core/graph_sync.py) can index and diff this project's subgraph.

The Python model differs from C# where the language does (see GRAPH_MODEL.md for the full
rationale and the per-label property model):

- No Namespace/File split -- a Python module *is* a file, so `Module` (per .py) and
  `Package` (per package dir) replace C#'s Namespace + File.
- `Function` (module-level def) exists alongside `Method` (class member), because
  Python has free functions and C# doesn't.
- `EXTENDS` is multi-valued (multiple inheritance) and the resolved `mro` rides on the
  Class node; `IMPLEMENTS` is emitted only when a base is an abc.ABC / typing.Protocol.
- Types are dynamic, so every type-bearing property is paired with a provenance marker
  (`type_source` on nodes, `resolution` on edges) -- see GRAPH_MODEL.md.
"""

from __future__ import annotations

from auryn.core.graph_model import EdgeSchema, NodeSchema

NODE_SCHEMAS = [
    NodeSchema(label="Project", indexed_properties=("name",)),
    # `linkage` (ADR 0004): the project-independent key on every externally-visible node, indexed
    # so cross-repo reconciliation resolves a placeholder<->definer by targeted `linkage IN [...]`
    # seeks (never a scan).
    NodeSchema(label="Package", indexed_properties=("name", "linkage_key")),
    NodeSchema(label="Module", indexed_properties=("name", "linkage_key")),
    NodeSchema(label="Class", indexed_properties=("name", "linkage_key")),
    NodeSchema(label="Function", indexed_properties=("name", "linkage_key")),
    NodeSchema(label="Method", indexed_properties=("name", "linkage_key")),
    NodeSchema(label="Attribute", indexed_properties=("name",)),
    NodeSchema(label="Parameter", indexed_properties=("name",)),
    # Deferred (see GRAPH_MODEL.md): a `DTO` classification for @dataclass /
    # pydantic BaseModel / TypedDict / NamedTuple, mirroring C#'s DTO label. Not emitted
    # by the first cut -- until then the `is_dataclass` flag on Class carries the signal.
    # NodeSchema(label="DTO", indexed_properties=("name",)),
]

EDGE_SCHEMAS = [
    # Containment backbone -- no properties, pure structure (as in C#).
    EdgeSchema(rel_type="HAS_PACKAGE"),  # Project  -> Package
    EdgeSchema(rel_type="HAS_SUBPACKAGE"),  # Package -> Package
    EdgeSchema(rel_type="HAS_MODULE"),  # Project|Package -> Module
    EdgeSchema(rel_type="HAS_CLASS"),  # Module|Package -> Class (Package for __init__.py symbols)
    EdgeSchema(rel_type="HAS_FUNCTION"),  # Module|Package -> Function
    EdgeSchema(rel_type="HAS_METHOD"),  # Class -> Method
    EdgeSchema(rel_type="HAS_ATTRIBUTE"),  # Class -> Attribute
    EdgeSchema(rel_type="HAS_PARAMETER"),  # Function|Method -> Parameter
    EdgeSchema(rel_type="HAS_NESTED_CLASS"),  # Class -> Class
    EdgeSchema(rel_type="HAS_NESTED_FUNCTION"),  # Function|Method -> Function (a def nested in a body)
    # Semantic layer -- every one carries a provenance property (see contract).
    EdgeSchema(rel_type="EXTENDS"),  # Class -> Class (local base); {resolution, mro_index}
    EdgeSchema(rel_type="IMPLEMENTS"),  # Class -> Class (local ABC/Protocol base); {resolution}
    # Function|Method -> Function|Method, PLUS Module|Package -> Function|Method for a script's
    # top-level (module-scope) call statements: the Module/Package node is the one non-callable
    # that can originate a CALLS edge (Python-specific; `main()` under `if __name__ ...` et al.).
    # {resolution, receiver_type, argument_types, type_arguments}
    EdgeSchema(rel_type="CALLS"),
    EdgeSchema(rel_type="RETURNS"),  # Function|Method -> Class (only when the return type resolves to a local class); {resolution}
    # Module|Package -> Module|Package, one per source module aggregating its imports of a
    # given project module; {names, resolution}. This is the module-level dependency
    # breadcrumb the incremental resync closure needs (ADR 0002): when a module changes,
    # its importers are found by walking IMPORTS in reverse -- catching a reference that
    # only now resolves because the imported module gained a name, which the symbol-level
    # CALLS/EXTENDS reverse-deps can't see (a not-yet-existing name has no node to point at).
    EdgeSchema(rel_type="IMPORTS"),
    # Deliberately NOT emitted:
    #   DECORATED_BY -- decorators ride as a `decorators` string list on the node; the
    #   edge form is what a later framework-enrichment pass (Django/FastAPI/Celery) adds.
]

# Every label that can be an EXTENDS/IMPLEMENTS/HAS_CLASS/HAS_NESTED_CLASS endpoint --
# i.e. every "class-like" declaration kind. Only `Class` today (`DTO` joins it if/when
# the classification label above is enabled). Kept as a named tuple for parity with the
# C# ingestor's TYPE_LABELS, which read_fingerprints()-style scans iterate over.
TYPE_LABELS = ("Class",)
