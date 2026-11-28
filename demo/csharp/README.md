# Demo: a small ASP.NET Core-style API (C# ingestor)

A hand-authored "Widgets" API -- a controller, a service behind an interface, a
repository interface, a few DTOs, an entity hierarchy, an enum, a record struct, and
a delegate -- sized to exercise every entity and classification rule the C# ingestor
supports, the same way the [PL/SQL demo](../plsql/README.md) is a small hand-built
schema rather than a downloaded dataset. Unlike the SQL/PL/SQL demos, there's no
external database to stand up: the C# ingestor reads `.cs` files directly, so this
one only needs FalkorDB.

## What's in it

`src/Controllers/WidgetsController.cs`, `src/Models/{Entities,Dtos}.cs`,
`src/Services/WidgetService.cs` -- four files, three namespaces
(`Acme.Widgets.{Controllers,Models,Services}`):

| Type | Namespace | Notes |
|---|---|---|
| `BaseEntity` (`Class`, abstract) | Models | `Id`, `CreatedAt` |
| `Widget` (`Class`) | Models | `: BaseEntity` -- an `EXTENDS` edge |
| `WidgetStatus` (`Enum`, `byte`) | Models | `Active`, `Retired` |
| `Money` (`Record`, `readonly record struct`) | Models | `is_value_type: true`, positional `(Amount, Currency)` |
| `WidgetChangedHandler` (`Delegate`) | Models | params reference `Widget`/`WidgetStatus` directly, not just primitives |
| `WidgetDto`, `CreateWidgetRequest`, `UpdateWidgetRequest` (`Class` in source) | Models | all three end up labeled `DTO`, not `Class` -- see below |
| `IWidgetRepository`, `IWidgetService` (`Interface`) | Services | |
| `WidgetRepository` (`Class`) | Services | `: IWidgetRepository` -- an in-memory implementation, so `FindById`/`Save` have real bodies to look at, not just the interface's bare signatures |
| `WidgetService` (`Class`) | Services | `: IWidgetService` -- an `IMPLEMENTS` edge |
| `WidgetsController` (`Class`) | Controllers | `[ApiController]`, `: ControllerBase` |

## What ingesting it produces

```bash
uv run auryn run csharp --path demo/csharp/src --project-name Widgets.Api --solution-name Acme
```

writes **83 nodes / 140 edges** (1 `Solution`, 1 `Project`, 5 `Namespace`, 4 `File`,
5 `Class`, 2 `Interface`, 3 `DTO`, 1 `Enum`/2 `EnumMember`, 1 `Record`, 1 `Delegate`,
16 `Method`, 11 `Property`, 4 `Field`, 23 `Parameter`, 3 `APIEndpoint`). A few things
worth looking at directly in the [viewer](../../README.md#viewing-ingested-graphs)
or via Cypher:

- **DTO classification, both ways it can happen**: `WidgetDto` is declared as a
  plain `class` with no special name -- it's only classified as `DTO` because
  `WidgetsController.GetById`/`Create`/`Update` all return it (the *definitive*
  rule: used as an API endpoint's parameter or return type). `CreateWidgetRequest`
  and `UpdateWidgetRequest` get the same `DTO` label two ways at once: they're also
  endpoint parameters *and* match the default `Request` name-suffix heuristic.
  `Widget` itself, despite being conceptually similar (it flows through
  `WidgetService` constantly), stays a plain `Class` -- it's never directly an
  endpoint parameter or return type (the DTOs are what cross that boundary) and its
  name doesn't match any suffix. Query it directly:
  ```cypher
  MATCH (d:DTO) RETURN d.name
  MATCH (c:Class) RETURN c.name
  ```
- **A `Method`'s `source` is exactly its own declaration, nothing more** --
  `IWidgetRepository.FindById`'s `source` is just `"Widget FindById(int id);"`,
  because that's *all there is*: an interface method has no body, only a signature.
  `WidgetRepository.FindById` (the concrete class implementing it) is the node with
  the real implementation:
  ```cypher
  MATCH (a)-[:IMPLEMENTS]->(b) RETURN a.name, b.name
  -- ("WidgetRepository", "IWidgetRepository"), ("WidgetService", "IWidgetService")
  MATCH (c:Class {name: 'WidgetRepository'})-[:HAS_METHOD]->(m:Method) RETURN m.name, m.source
  ```
- **`WidgetsController.GetById` calls `_service.GetById` -- a same-named method on a
  *different* type, not itself.** Naively resolving "a call to `GetById` from inside
  a method named `GetById`" by name alone would wrongly loop the node back onto
  itself; the ingestor instead resolves the call's receiver (`_service`, a field
  typed `IWidgetService`) to that field's declared type first, landing the
  `DEPENDS_ON {kind: "CALL"}` edge on `IWidgetService.GetById` instead:
  ```cypher
  MATCH (a:Method {name: 'GetById'})-[:DEPENDS_ON {kind: 'CALL'}]->(b:Method {name: 'GetById'})
  MATCH (a)<-[:HAS_METHOD]-(fromType), (b)<-[:HAS_METHOD]-(toType)
  RETURN labels(fromType)[0], labels(toType)[0]
  -- ["Class", "Interface"], never ["Class", "Class"] pointing at itself
  ```
- **Route templates are actually combined, not just copied**: the controller-level
  `[Route("api/[controller]")]` and each method's `[HttpGet("{id}")]`/etc. combine
  into one `route` property per `APIEndpoint`, with `[controller]` substituted for
  the controller's own name (`WidgetsController` minus the `Controller` suffix):
  ```cypher
  MATCH (m:Method)-[:EXPOSES_ENDPOINT]->(e:APIEndpoint)
  RETURN m.name, e.http_method, e.route
  -- ("GetById", "GET", "api/Widgets/{id}"), ("Create", "POST", "api/Widgets"), ...
  ```
- **`WidgetService.Create`/`GetById`/`Update` all call the same private
  `ToDto` helper** -- a same-type, bare (unqualified) call, resolved through a
  different path than the `_service.GetById` case above (no receiver to look up a
  field type for; same-type-name match is trusted here specifically because there's
  no receiver at all, not despite it).
- **`WidgetChangedHandler`'s parameters reference `Widget` and `WidgetStatus`
  directly** (`HAS_PARAMETER`, not `DEPENDS_ON`) -- a concrete illustration of
  `Delegate` reusing the same parameter-modeling machinery as `Method`, per the
  ingestor's entity model.

Re-running the ingestion command is safe -- it's idempotent, so you'll still see
83 nodes / 140 edges rather than duplicates.

## Run it

```bash
uv run python demo/csharp/run_demo.py
```

One command: starts FalkorDB via Docker if nothing's already listening on its
default port (reuses it on later runs instead of creating a duplicate), ingests
`src/`, starts the viewer, and opens `http://127.0.0.1:8080` in your browser
automatically. Ctrl-C stops the viewer when you're done.

### Manual steps

If you'd rather drive it yourself, `run_demo.py` is just gluing these together:

```bash
uv run auryn run csharp \
    --path demo/csharp/src \
    --project-name Widgets.Api \
    --solution-name Acme

uv run auryn view
```

Then open `http://127.0.0.1:8080` and pick the `auryn` graph.
