---
name: auryn
description: "Use for any structural question about a codebase — where a function/class/module is defined; who calls X or what X calls; everything that depends on a symbol; what will break, what's affected, or how risky it is to change or refactor a function, type, or field (change impact / blast radius); the call path between two functions (does X reach Y, and how); or tracing what an entry point / endpoint / function drives end to end. Prefer this over grepping and reading whole files: it queries a persistent code graph via the `auryn` MCP server and returns the precise answer -- with file:line and the code at each call site -- in a single call, without pulling files into context. If the `auryn_*` tools aren't already listed, they're deferred — load them first with the ToolSearch tool (query \"auryn\")."
---

# Auryn

The working repository can be indexed into a queryable code graph (FalkorDB) exposed by the
`auryn` MCP server. **The graph is the map — use it to *find*, not to *search*.** A
single query returns the exact structural answer with ids; reading and grepping whole files
to reconstruct the same fact is slower, costs far more tokens, and is easy to get wrong.

## When to reach for it

Any question about structure or relationships: "where is X", "who calls X / what depends
on it", "what does X call / depend on", "does X reach Y, and how", "what breaks if I change
X", "what does this endpoint/handler do end to end", "list the public API / entry points".

## Workflow

0. **Load the tools if needed.** If the `auryn_*` tools aren't in your tool list, they're
   deferred — call the `ToolSearch` tool (query "auryn", or
   `select:mcp__auryn__locate,mcp__auryn__who_calls,mcp__auryn__impact_of`) to load them first.
1. **`locate(name=...)`** — find the symbol; or **`search_code(query=...)`** when you don't
   know its name. A `locate` result already has `file_path`, `line_start`/`line_end` and
   `signature`, plus the `id` every other tool takes. A `search_code` hit has the `id`,
   `qualified_name` and a `snippet` only: call `get_context(id)` before citing its location.
2. Answer with the smallest tool that fits:
   - **`who_calls(id)`** / **`callees(id)`** — callers / callees. Every edge carries the call
     `lines` and `sites` (`{line, code}`) in the caller's `file`: that is your file:line
     evidence. `max_depth` for transitive.
   - **`get_context(id)`** — a compact view: location, signature, a numbered preview, and
     callers/callees with call sites. **`get_context(id, lines="a-b")`** returns exactly those
     numbered source lines, so you rarely need to open the file.
   - **`find_path(from_id, to_id)`** — how two symbols connect (`edge_types=["CALLS"]` for a
     pure call chain).
   - **`impact_of(id, direction=...)`** — change blast radius across "uses" edges.
   - **`trace_entrypoint(id)`** — the full call-closure of a handler, function or
     `APIEndpoint`.
3. If a query comes back empty, **`auryn_status`** tells you whether the graph has data;
   **`index_repository(path=".")`** builds it once.

## Rules

- Prefer a graph query over grepping; cite the `file_path:line` and code the answer gives.
- Open a file only for lines the answer doesn't show.
- The graph is honest about confidence: call/inheritance edges carry `resolution`
  (resolved | inferred | external). Trust `resolved` over `inferred`, and say so when it
  matters. Check `truncated` on traversal results before calling them complete.
- `AURYN_MCP_TOOLS=all` adds the admin/escape-hatch tools (`list_graphs`, `use_graph`,
  `get_schema`, `run_cypher`, `impact_of_diff`, `ingest_repository`).
