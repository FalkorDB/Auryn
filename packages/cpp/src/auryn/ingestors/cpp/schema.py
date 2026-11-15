"""Graph schema (node/edge labels) the C++ ingestor writes. git_sync passes NODE_SCHEMAS to
setup_schema()/read_fingerprints() so the delta-sync pipeline (core/graph_sync.py) can index
and diff this project's C++ subgraph.

C++ is the structural union of the C and Java ingestors, so the schema is too: from Java, a
namespace tier (`Namespace`, chained by HAS_NAMESPACE/HAS_SUB_NAMESPACE) over types with
methods/fields and a class hierarchy (EXTENDS); from C, free `Function`s, file-scoped `Macro`s,
and File->File `INCLUDES`. Language-specific facts are kept as node/edge properties rather than
new labels: `enum class` sets `is_scoped` on the Enum, a template sets `type_parameters`/
`is_template` on the type (its arity is also folded into the id so `Box`/`Box<T>` don't
collide), and EXTENDS carries the base's `access` (C++ has multiple inheritance).

Differences from Java that track the language: the tier is `Namespace` (not `Package`); there
are free `Function`s (Java has none); `Struct`/`Union` are first-class type labels; and there is
**no IMPLEMENTS** edge -- C++ has no interfaces, so deriving from an abstract/pure-virtual class
is still EXTENDS.

The shared cross-ingestor contract is honored exactly: callables are `Function`/`Method`, the
call edge is `CALLS` (carrying a `resolution` provenance), and `Parameter`/`HAS_PARAMETER`/
`EXTENDS`/`DEPENDS_ON`/`DEFINED_IN` match the C/Java/C#/Python ingestors."""

from __future__ import annotations

from auryn.core.graph_model import EdgeSchema, NodeSchema

NODE_SCHEMAS = [
    NodeSchema(label="Project", indexed_properties=("name",)),
    NodeSchema(label="Namespace", indexed_properties=("name",)),
    NodeSchema(label="File", indexed_properties=("path",)),
    NodeSchema(label="Class", indexed_properties=("name",)),
    NodeSchema(label="Struct", indexed_properties=("name",)),
    NodeSchema(label="Union", indexed_properties=("name",)),
    NodeSchema(label="Enum", indexed_properties=("name",)),
    NodeSchema(label="Function", indexed_properties=("name", "linkage_key")),  # linkage_key: ADR 0004 cross-repo
    NodeSchema(label="Method", indexed_properties=("name",)),
    NodeSchema(label="Field", indexed_properties=("name",)),
    NodeSchema(label="Parameter", indexed_properties=("name",)),
    NodeSchema(label="EnumMember", indexed_properties=("name",)),
    NodeSchema(label="Macro", indexed_properties=("name",)),
]

EDGE_SCHEMAS = [
    # Containment backbone -- pure structure, no properties (as in C#/Java/Python).
    EdgeSchema(rel_type="HAS_NAMESPACE"),      # Project -> Namespace (first segment)
    EdgeSchema(rel_type="HAS_SUB_NAMESPACE"),  # Namespace -> Namespace
    EdgeSchema(rel_type="HAS_FILE"),           # Project -> File
    EdgeSchema(rel_type="HAS_TYPE"),           # Namespace|Project -> Class|Struct|Union|Enum
    EdgeSchema(rel_type="HAS_NESTED_TYPE"),    # type -> type
    EdgeSchema(rel_type="HAS_FUNCTION"),       # Namespace|Project -> Function (free function)
    EdgeSchema(rel_type="DEFINED_IN"),         # type -> File
    EdgeSchema(rel_type="HAS_METHOD"),         # type -> Method
    EdgeSchema(rel_type="HAS_FIELD"),          # type -> Field
    EdgeSchema(rel_type="HAS_PARAMETER"),      # Method|Function -> Parameter
    EdgeSchema(rel_type="HAS_ENUM_MEMBER"),    # Enum -> EnumMember
    # Per-file provenance handle (uniform with C/Go/Java, for the authored-shared incremental engine,
    # ADR 0005): File -> every program-global entity it DEFINES (a type's class body, a method/free-
    # function body, a macro) or merely DECLARES (a forward `class X;`, an in-class method declaration
    # whose body is out-of-line, a free-function prototype, or a namespace a file only contributes to).
    # DEFINES owns the definition data (props/lateral edges/children); DECLARES is the lighter handle a
    # header carries. Both also exist on types/functions as DEFINED_IN/HAS_FUNCTION for the cross-ingestor
    # contract -- DEFINES/DECLARES add the File->entity direction the delta engine reads ownership from.
    EdgeSchema(rel_type="DEFINES"),            # File -> Macro|Type|Method|Function|Namespace (definition site)
    EdgeSchema(rel_type="DECLARES"),           # File -> Type|Method|Function (declaration-only site)
    EdgeSchema(rel_type="INCLUDES"),           # File -> File (#include resolved within the project)
    # Semantic layer -- each cross-reference carries a {resolution} provenance where applicable.
    EdgeSchema(rel_type="EXTENDS"),      # Class|Struct -> Class|Struct ; {access}. No IMPLEMENTS (no interfaces).
    EdgeSchema(rel_type="CALLS"),        # Method|Function -> Method|Function ; {resolution, receiver_type?}
    EdgeSchema(rel_type="DEPENDS_ON"),   # Method|Field|Function -> type (param/return/field type refs)
]

# Every label that can be an EXTENDS/HAS_TYPE/DEPENDS_ON type endpoint.
TYPE_LABELS = ("Class", "Struct", "Union", "Enum")
