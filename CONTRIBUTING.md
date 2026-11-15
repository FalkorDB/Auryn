# Contributing

Thanks for contributing to Auryn. This page is the quick map; the deep
dives live under [`docs/`](docs/README.md).

## Project shape

The project is a [uv workspace](https://docs.astral.tv/uv/concepts/workspaces/): a core
distribution plus one plugin distribution per language.

```
packages/
├── core/          # auryn-core: registry, delta-sync, MCP server, viewer, CLI, graph client
├── go/            # auryn-go   ┐
├── python/        # auryn-python │  one auryn-<lang> plugin per language,
├── csharp/        # auryn-csharp │  discovered via entry points (never a central list)
├── oracle/        # auryn-oracle │
└── …              #                ┘
```

Get set up and run the suite:

```bash
uv sync                                              # installs the whole workspace, editable
uv run pytest --cov=auryn --cov-report=term-missing
```

Tests run against an **embedded** FalkorDB by default (no server or Docker needed). See the
[README's Development section](README.md#development) for the backend matrix.

## How extension works

Nothing central lists the ingestors. Each plugin declares itself with an **entry point** in
its own `pyproject.toml`, and core discovers them at runtime. There are three groups, by
concern:

| Entry-point group | For | Registered with | Reached from |
|---|---|---|---|
| `ingestor.languages` | file-based language ingestors (most contributions) | `register_git_language()` | `ingest dir`/`ingest repo`, the `ingest_*` MCP tools |
| `ingestor.ingestors` | live-source ingestors (a DB connection) | `@register_ingestor("name")` | `auryn run <name>` |
| `ingestor.enrichments` | passes that derive edges from the finished graph | `register_graph_enrichment()` | run after the languages by the ingest endpoint |

Adding an ingestor means **creating a new `packages/<name>/` distribution** and declaring
its entry point — plus wiring the distribution into the root `pyproject.toml` (four spots:
the extras, `[tool.uv.sources]`, the dev group, and coverage). You never edit a central
registry or `cli.py`.

## Adding a new language ingestor

This is the common case and has its own thorough, worked walkthrough:

### 👉 [docs/adding-a-language.md](docs/adding-a-language.md)

It uses the **Go plugin (`packages/go/`)** as a line-by-line reference and covers the
package layout, the import-light `plugin.py`, the two-phase parse→resolve pipeline, the id
scheme, the graph schema and cross-ingestor contract, the delta-sync engine, cross-repo
linkage (ADR 0004), a deep second pass, workspace wiring, and tests.

Before writing code, skim two things:

- **[docs/graph-model.md](docs/graph-model.md)** — the shared node/edge vocabulary. Reuse
  `Function`/`Method`, `CALLS` (with a `resolution` property), `Parameter.type`,
  `EXTENDS`/`IMPLEMENTS`/`DEPENDS_ON` where they fit, so cross-language queries work.
- **[docs/architecture.md](docs/architecture.md)** — how discovery, the ingest pipeline,
  and delta-sync fit together.

## Design principles

- **Ids are your idempotency key.** Every node/edge is written with `MERGE` keyed on its id
  (`NodeSchema.key_property`, default `"id"`). Re-running must produce the *same* id for the
  *same* source symbol, or you get duplicates instead of updates and the delta sync breaks.
  Ids embed names and structural position, **never resolved/inferred types**.
- **Recompute and diff, don't append.** A sync recomputes the whole target state and diffs
  it against the graph (`compute_delta` → `apply_delta`). This is why an unchanged resync is
  a no-op and moving to any commit/branch is the same operation.
- **Emit no edge rather than a wrong one.** The fast (tree-sitter) pass is name-based; when
  a reference is ambiguous or unresolvable, drop it. A present edge is trustworthy. Anything
  needing real types (structural `IMPLEMENTS`, inferred calls) belongs in an optional deep
  pass, not a guess in the fast one.
- **Indexes/constraints are automatic.** `setup_schema(node_schemas)` runs before any write,
  so a fresh FalkorDB gets the right indexes on first run. Don't add a separate setup step.
- **Reuse labels when linking into another ingestor's nodes.** E.g. PL/SQL `Trigger` nodes
  attach onto SQL `Table` nodes by reusing its exact id scheme, rather than inventing a
  parallel one. If your ingestor depends on another's data existing first, check for it
  (`GraphWriter.node_exists`) and warn — don't silently produce dangling edges.
- **Logging**: use `logging.getLogger(f"ingestor.{self.name}")` (or `__name__` in a
  plugin). `BaseIngestor.run()` already logs start/summary for you.

## The C# extractor has two engines

C# ingestion runs on either engine behind the same graph model and id scheme: **Roslyn**
(default; a .NET tool in `tools/roslyn-extractor/` doing semantic analysis + DI resolution)
and **tree-sitter** (the syntax-only fallback). Both must emit the **identical** id scheme
so their output is interchangeable in one graph — `tests/ingestors/csharp/test_roslyn.py`
asserts that parity, and changing the scheme means changing it in both places (Python
`resolution.py` and its C# twin `tools/roslyn-extractor/Ids.cs`). The Roslyn tool needs the
.NET SDK; its tests skip when `dotnet` is absent.

## Pull requests

- Keep new code covered — run the coverage command above and confirm your lines are hit.
- Match the surrounding style (naming, docstrings, comment density). The existing ingestors
  are consistent on purpose; a new one should read like a sibling.
- Note anything you deliberately left to a future pass (like a deep semantic engine) in the
  module docstring, the way the current ingestors do.
</content>
