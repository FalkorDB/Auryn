# Python ingestor demo

Shows the deep, in-process semantics the Python ingestor produces — inferred
variable/attribute types, resolved cross-file inheritance (MRO), and resolved-vs-inferred
calls — the understanding a plain `ast`/tree-sitter parser can't give.

## Install

Everything is a plain Python dependency — no Node, no build step, no vendored toolchain:

```bash
uv sync
```

The default deep engine is [Jedi](https://jedi.readthedocs.io/) (pure-Python, in-process).
The stdlib-`ast` engine is the zero-dep fallback (auto-selected only if Jedi is somehow
unimportable; also selectable explicitly).

## Run

```bash
uv run python demo/python-auryn/run_demo.py
```

Docker is only needed if FalkorDB isn't already on `localhost:6379`. The demo ingests
`sample/app/` through the real multi-language endpoint (`ingest_directory`, Jedi engine),
narrates the graph, and opens the viewer.

## What it demonstrates

Over `sample/app/` (three modules: `models.py`, `db.py`, `widget.py`):

1. **Cross-file inheritance with a resolved MRO** — `Widget(Entity)` where `Entity` lives
   in another module, plus the ordered `mro`.
2. **The three-way call split** — `render()` calls `get_connection()` (resolved edge),
   `conn.execute()` (**inferred** edge, receiver type `Connection` deduced), and
   `json.dumps()` (external → dropped).
3. **Inferred types with provenance** — `self.name → str`, `self.tags → list`,
   `self → Widget` all inferred in-process; each `Attribute`/`Parameter` carries
   `type_source` ∈ {annotated, inferred, unresolved}, honest about what's known vs. guessed.

See `../../src/auryn/ingestors/python/GRAPH_MODEL.md` for the full node/edge model.
