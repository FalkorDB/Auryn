---
name: auryn
description: "Use for any structural question about this codebase — where a function/class/module is defined, who calls X, what X calls, the call path between two symbols, the blast radius of a change, or what an endpoint/handler does end to end. Prefer this over grepping and reading whole files: it queries a persistent code graph via the `auryn` MCP server and returns the precise answer (with node ids) in a single call, without pulling files into context."
---

# Auryn

This repository is indexed into a queryable code graph (FalkorDB) exposed by the
`auryn` MCP server. **The graph is the map — use it to *find*, not to *search*.** A
single query returns the exact structural answer with ids; reading and grepping whole files
to reconstruct the same fact is slower, costs far more tokens, and is easy to get wrong.

## When to reach for it

Any question about structure or relationships: "where is X", "who calls X / what depends
on it", "what does X call / depend on", "does X reach Y, and how", "what breaks if I change
X", "what does this endpoint/handler do end to end", "list the public API / entry points".

## Workflow

1. **`auryn_status`** — is the graph built for this repo? If `has_data` is false, run
   **`index_repository(path=".")`** once (a one-time cost; every query after is cheap).
2. **`locate(name=...)`** — turn a symbol name into its `id`; every other tool takes that
   `id`, never a display name.
3. Answer with the smallest tool that fits:
   - **`who_calls(id)`** — who calls this (inward); **`callees(id)`** — what this calls
     (outward). `max_depth` for transitive.
   - **`find_path(from_id, to_id)`** — the shortest path between two symbols (any edge
     type; pass `edge_types=["CALLS"]` for a pure call chain).
   - **`impact_of(id, direction=...)`** — change blast radius across "uses" edges.
   - **`behavioral_inventory(id)`** — the full call-closure of an entry point; pass a
     handler, a procedure/function, or an `APIEndpoint` (endpoints resolve to their handler
     first, so "what does this endpoint do end to end" works).
   - **`get_context(id)`** — one node + its 1-hop neighbours + `source` text.
4. **`get_schema`** (labels/edge-types) + **`run_cypher`** (arbitrary READ-ONLY Cypher) —
   the escape hatch for anything the curated tools don't cover.

## Rules

- Prefer a graph query over reading files; one query beats grepping several files.
- Cite the node `id` / file path the graph returns as your evidence.
- The graph is honest about confidence: type properties carry `type_source`
  (annotated | inferred | unresolved) and call/inheritance edges carry `resolution`
  (resolved | inferred | unresolved). Trust `resolved`/`annotated` over `inferred`, and say
  so when it matters. Check `truncated` on traversal results before calling them complete.
