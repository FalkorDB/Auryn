# Auryn — architecture

*A plain-English description of how Auryn works, for a website "How it works" section and an
architecture diagram. Accurate to the current implementation.*

## One sentence

Auryn turns a codebase into a **queryable knowledge graph** in a real graph database (FalkorDB),
and exposes it to your AI coding assistant through an **MCP server** — so the assistant *traverses and
cites* structure instead of grepping and guessing.

## The pipeline (source → graph → agent)

```
   your repo / working dir
          │
          ▼
 ┌──────────────────────┐   auto-detects each language present
 │  Language ingestors  │   Python · TypeScript/JS · C# · Java · C · PL/SQL · SQL
 │  (tree-sitter +      │   fast structural pass, then a deeper semantic pass where available
 │   deep engines)      │
 └──────────┬───────────┘
            │  nodes (functions, classes, modules, tables, endpoints…)
            │  edges  (CALLS, IMPORTS, EXTENDS, READS/WRITES, DEPENDS_ON…)
            ▼
 ┌──────────────────────┐   one graph per repository (keyed automatically)
 │      FalkorDB        │   real property graph + Cypher; idempotent MERGE writes
 │   (local or remote)  │   git-tracked: re-sync computes a precise delta (blob-sha parse cache)
 └──────────┬───────────┘
            │  GRAPH.RO_QUERY (read-only) for every query tool
            ▼
 ┌──────────────────────┐            ┌───────────────────────────┐
 │     MCP server       │◀──────────▶│   Your AI coding agent    │
 │  curated graph tools │   stdio    │  (Claude Code, …)         │
 └──────────────────────┘            └───────────────────────────┘
            ▲
            │  same read layer
 ┌──────────────────────┐
 │   Web graph viewer   │  explore visually: communities, path-finder, schema
 └──────────────────────┘
```

## The four parts

**1. Language ingestors (extraction).** Each supported language has its own ingestor. They share a
two-phase design: a **fast structural pass** with tree-sitter (definitions, imports, references —
no build or SDK needed), then an optional **deep semantic pass** where a real analyzer exists:
- **C#** → Roslyn: full semantic model with dependency-injection resolution (interface calls resolve
  to concrete implementations), real base-type kinds for `EXTENDS`/`IMPLEMENTS`.
- **Python** → Jedi: inferred types and receiver-resolved calls.
- **PL/SQL** → a vendored ANTLR grammar: `CALLS`/`READS`/`WRITES` parsed from procedure bodies.

Ingestion runs over a git repo **or** a plain directory, in parallel across a process pool, and is
**idempotent** — re-ingesting unchanged content is a no-op; a changed file produces a precise delta.

**2. FalkorDB graph (storage).** A real property graph, not a JSON file or a vector index. Nodes carry
properties (and source text where captured); edges carry a `resolution` tag. You (or the agent) can run
arbitrary read-only **Cypher**. Two ways to run it:
- **Local** — a bundled, serverless FalkorDB (falkordb-lite) that Auryn starts on demand. No
  server, no Docker.
- **Remote** — point at an existing/shared FalkorDB (host/port/creds).

**3. MCP server (agent access layer).** Exposes a curated, bounded, evidence-backed tool set over one
graph: check status, locate a symbol, who-calls / callees, shortest path, change-impact, the full
behavioral closure of an entry point, one-node context, the schema, and raw read-only Cypher — plus
`index_repository` to build the graph on first use. Every query tool is *structurally* read-only
(`GRAPH.RO_QUERY`), so it cannot mutate the graph regardless of input.

**4. Web viewer (human exploration).** A WebGL graph explorer over the same read layer: colour by
category or by **detected community** (subsystems), a **path-finder** that traces the shortest
connection between two symbols, a schema overview, and git-tracked project sync.

## What makes the graph trustworthy

- **Provenance on every edge/type.** Call and inheritance edges carry `resolution`
  (`resolved` | `inferred` | `unresolved`); type properties carry `type_source`
  (`annotated` | `inferred` | `unresolved`). The agent is told to trust `resolved`/`annotated` over
  `inferred` — and to say so.
- **Citations, not vibes.** Answers reference the node `id` and file path the graph returns.
- **Structure, not similarity.** Lookups follow real edges (a call graph, an import graph), so results
  are exact and complete-by-construction — not the top-k nearest vectors.

## Cross-tier "seams" (beyond single-language graphs)

For real applications the interesting edges cross language boundaries. Auryn models these where a
generic tool can't: a Kendo/ASP.NET MVC **UI event → C# controller** (`CALLS_API`/`SUBMITS_TO`), and a
C# message **dispatched to a PL/SQL package function** via a routing table (`DISPATCHES_TO`). The result
is an unbroken trace: **UI → C# → PL/SQL → tables**.

## Scale & freshness

Proven against a real, large codebase (`dotnet/aspnetcore` — hundreds of thousands of nodes/edges). A
per-blob-sha parse cache means a full release's worth of changes re-parses a handful of files, not the
whole repo, so keeping the graph current is cheap.

## Honest boundaries (state these — they build trust)

- Some ingestors store a file path and/or full source text but **not yet line ranges**.
- C# and PL/SQL are two subgraphs joined only at the modeled seams above — traversals don't silently
  cross elsewhere.
- **No embeddings / semantic search** — Auryn is structural on purpose. (Great for "who calls X",
  not for "find code that feels like Y".)
