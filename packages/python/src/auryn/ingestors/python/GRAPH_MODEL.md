# Python graph model

The node/edge model both Python engines emit into the shared FalkorDB graph:

- **`ast`** — stdlib-only, zero extra deps. Structure + statically-resolvable
  inheritance/calls; types are `annotated` or `unresolved`.
- **`jedi`** (default) — layers [Jedi](https://jedi.readthedocs.io/)'s in-process static
  inference on top: inferred types and resolved calls, including receiver-typed ones.

Both use the same id scheme (`ids.py`) and the same labels/properties below, so their
output is interchangeable for the backbone; they differ only in how much they resolve,
which the provenance fields record honestly. Jedi is a pure-Python pip dependency — the
whole install is `uv sync`, no second runtime.

## Id scheme (`ids.py`)

Extends the C# `.kind.name` convention. Root is `python://{project}`.

| Kind | Id |
|---|---|
| Project | `python://{project}` |
| Package | `python://{project}/package/{dotted.pkg}` |
| Module | `python://{project}/module/{dotted.module}` |
| Class | `{module_id}.class.{name_lower}` |
| Function (module-level) | `{module_id}.func.{name_lower}({param_names})` |
| Method (class member) | `{class_id}.method.{name_lower}({param_names})` |
| Field | `{class_id}.attr.{name_lower}` |
| Parameter | `{owner_id}.param.{position}.{name_lower}` |

### Id-stability rule

Method/function ids embed parameter **names**, never inferred types. Types are volatile
across re-syncs (inference shifts as code around them changes); a type in an id would churn
the id and storm the delta. Param names are stable source facts, and they also disambiguate
`@property`/`@x.setter` pairs and `@overload` stubs.

## Provenance vocabulary

Because the ingestor is generic (0%→100% annotated in the same repo), every type/edge says
*how we know it*:

- **`type_source`** on any type-bearing node property — `"annotated"` (a written hint),
  `"inferred"` (an engine deduced it — Jedi only), `"unresolved"` (unknown).
- **`resolution`** on semantic edges — `"resolved"` (statically bound), `"inferred"` (bound
  via receiver-type inference — Jedi only), `"constructor"` (a CALLS edge standing for `Cls()`
  instantiation, redirected to the class's `__init__`), `"external"` (a nameable stdlib/
  third-party/sibling-repo symbol → a placeholder node, ADR 0004), `"unresolved"`.

The `ast` engine never emits `inferred`; that value is the Jedi engine's signal. (`constructor`
and `external` are emitted by both engines.)

## Nodes

Common: `line_start`, `line_end`. `Package`/`Module` carry `name` + `dotted_path` (+
`file_path` on Module). `Class`/`Function`/`Method` also carry `source` — the entity's
verbatim source text (the shared `source` property that the C / C# / PL-SQL ingestors also
write, so `get_context` returns an entity's code uniformly across languages; it is stripped
from lean list results by `serialize_node_lean`).

- **Class** — `name`, `qualified_name`, `bases` (declared), `mro` (resolved, ordered),
  `unresolved_bases`, `is_abstract`, `is_protocol`, `is_dataclass`, `decorators`, `source`.
- **Function / Method** — `name`, `qualified_name`, `signature`, `return_type`,
  `return_type_source`, `is_async`, `is_generator`, `decorators`, `unresolved_calls`,
  `source`. Method also: `kind` (`instance|static|class|property`), `is_abstract`,
  `overrides`.
- **Field** — `name`, `type`, `type_source`, `defined_in` (`class_body` | method name),
  `is_classvar`.
- **Parameter** — `name`, `type`, `type_source`, `kind`, `position`, `has_default`.

*(Deferred: a `DTO` classification for `@dataclass`/pydantic/`TypedDict`; the `is_dataclass`
flag carries the signal until then.)*

## Edges

Containment (`HAS_PACKAGE`/`HAS_SUBPACKAGE`/`HAS_MODULE`/`HAS_TYPE`/`HAS_FUNCTION`/
`HAS_METHOD`/`HAS_FIELD`/`HAS_PARAMETER`/`HAS_NESTED_TYPE`) carry no properties.

| Edge | properties |
|---|---|
| `EXTENDS` | `resolution`, `mro_index` |
| `IMPLEMENTS` | `resolution` — when the local base is an ABC/Protocol |
| `CALLS` | `resolution`, `receiver_type`, `argument_types`, `type_arguments` |
| `RETURNS` | `resolution` — only when the return type resolves to a local class |

A `CALLS` edge normally originates at a `Function`/`Method` and targets a `Function`/`Method`.
Refinements:
- **Constructor targets.** `Cls()` edges to the class's `__init__` `Method` when it defines one,
  else to the **`Class`** node itself (`resolution="constructor"`) — so a class with no local
  `__init__` (a default or a purely inherited constructor) still records the construction. The
  `Class` node is therefore also a valid `CALLS` target.
- **Module-scope calls.** A module's top-level call statements — a script's driver code, e.g.
  `main()` under `if __name__ == "__main__":` — originate a `CALLS` edge from the `Module` (or
  `Package`, for an `__init__.py`) node itself.
- **Lambdas & nested defs.** Calls inside a `lambda` are attributed to the **enclosing** scope (a
  lambda is anonymous — it has no node of its own). A nested `def` *is* its own `Function` node
  (reached via `HAS_NESTED_FUNCTION`), so its calls originate there and `who_calls` finds callers
  inside closures / factory-registered handlers. Calls inside a nested `class`/method belong to
  that node.

(`argument_types`/`type_arguments` stay `[]` as for any call.)

Endpoints of semantic edges are **always project-local nodes**; external/stdlib types live
only in string properties (`bases`, `mro`, `return_type`).

### The three-way split for calls

A call site becomes exactly one of:

1. **In-project target** → a `CALLS` edge (`resolved` for a bare name, `inferred` when the
   target came from receiver-type inference — the Jedi-only case, e.g. `conn.execute()`).
2. **External** (`json.dumps`, stdlib, third-party) → **dropped** (out of graph scope).
3. **Dynamic / unresolvable** (`getattr`, computed dispatch) → recorded on the caller's
   **`unresolved_calls`** property, not an edge. The `ast` engine leaves this empty (it
   can't tell dynamic from external without types); the Jedi engine populates it.

## Worked example

Source (`demo/python-auryn/sample/app/`, project `demo`):

```python
# app/models.py         # app/db.py                       # app/widget.py
class Entity:           class Connection:                  from app.models import Entity
    def __init__(self,      def execute(self, sql: str):   from app.db import Connection, get_connection
                 id: int):      ...
        self.id = id                                       class Widget(Entity):
                        def get_connection() -> Connection:    def __init__(self, id, name: str):
                            ...                                    self.name = name   # inferred str
                                                                   self.tags = []     # inferred list
                                                               def render(self):
                                                                   conn = get_connection()  # resolved call
                                                                   conn.execute(self.name)  # inferred call
                                                                   json.dumps(self.tags)    # external -> dropped
```

What the **Jedi engine** emits for the interesting slice (backbone elided):

```
Class Widget         mro=[app.widget.Widget, app.models.Entity, object]
Field Widget.name   type=str  type_source=inferred
Field Widget.tags   type=list type_source=inferred
Parameter Widget.__init__.id   type=Unknown type_source=unresolved   # unannotated + no call site in these 3 files
Parameter render.self          type=Widget  type_source=inferred
# (in the full sample, app/api.py's `Widget(widget_id, ...)` with `widget_id: int`
#  lets Jedi infer that `id` param as `int`/inferred -- cross-file call-site inference)

Widget -EXTENDS {resolution:resolved, mro_index:1}-> Entity
render -CALLS {resolution:resolved}-> get_connection
render -CALLS {resolution:inferred, receiver_type:Connection}-> Connection.execute
get_connection -RETURNS {resolution:resolved}-> Connection
# json.dumps: no node, no edge (external)
```

The **ast engine** emits the same backbone + `EXTENDS`/`get_connection`/`RETURNS`, but the
attribute/param types are `unresolved` (no inference) and there is **no** `conn.execute`
edge (receiver-typed — needs inference). That gap is exactly what Jedi fills.

## Web-framework enrichment (endpoints & the golden path)

A registered graph-enrichment pass (`ingestors/pyframework/`, name `python-routes`) runs
after the language engines and turns route **decorators** already captured on
Function/Method nodes into entry points — no source re-parsing:

- `@app.post("/widgets")`, `@router.get("/x/{id}")` (FastAPI/Starlette/Flask 2 verb style)
  and `@app.route("/x", methods=["GET","POST"])` (Flask; one endpoint per method) →
- an **`APIEndpoint`** node (`route`, `http_method`, `framework`, `provenance:"pyframework"`)
  reusing the C# tier's label, plus a **`(handler Function|Method)-[:EXPOSES_ENDPOINT]->(APIEndpoint)`** edge (same direction as C#).

The pass is idempotent and provenance-scoped, so re-runs rebuild only its own layer and any
C# `APIEndpoint`s in the same graph are untouched.

**Why it matters — the golden path.** Because the endpoint reuses the shared label/edge, the
agent-access layer treats a Python route as a first-class entry point:
`trace_entrypoint` now resolves an `APIEndpoint` entry to its handler(s) via
`EXPOSES_ENDPOINT`, then returns the handler's full `CALLS` closure. So for the demo's
`app/api.py`:

```
POST /widgets  -EXPOSES_ENDPOINT<-  create_widget()  -CALLS->  get_connection(), Connection.execute()
trace_entrypoint(POST /widgets) -> { create_widget, get_connection, execute }
```

i.e. request → handler → call closure is one traceable path. (`impact_of` stays generic:
it already traverses `CALLS` from any handler/function.) Framework *dynamic dispatch*
beyond decorator routes — Django `urls.py`, DI/`Depends`, Celery tasks — is not yet
modelled; those are future enrichment passes.
