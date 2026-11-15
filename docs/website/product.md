# Auryn — "Product" page content

*Deeper than the homepage: what it is, how it works, use cases, comparisons, and honest limits.
Mirrors Graphify's Product/Concepts pages.*

---

## What is Auryn?

Auryn is an **open-source code knowledge graph for AI coding assistants**. It reads a codebase and
builds a graph where the **nodes** are the things in your code — functions, classes, modules, packages,
database tables, API endpoints — and the **edges** are the relationships between them — calls, imports,
inheritance, reads/writes, dependencies. Your assistant then answers questions by *walking that graph
and citing what it finds*, instead of grepping files and inferring.

It runs on your machine, stores the graph in a real graph database (FalkorDB), and plugs into your agent
over MCP.

---

## Core concepts

- **Knowledge graph** — data modeled as entities + relationships, so you can answer a question by
  *following connections* rather than matching text.
- **Code graph** — that idea applied to source: definitions become nodes, and calls/imports/references
  become edges you can traverse.
- **Community** — a cluster the graph detects automatically; it usually corresponds to a real subsystem
  (a module, a feature area). The viewer can colour and name them.
- **Provenance / confidence** — every relationship is tagged by how it was derived: `resolved` (a real
  analyzer confirmed it), `inferred` (a best-effort guess), or `unresolved`. Types are similarly
  `annotated` / `inferred` / `unresolved`. Grounded answers over confident-sounding ones.
- **Blast radius / impact** — everything reachable from a symbol across "uses" edges: what a change can
  affect.
- **Behavioral inventory** — the full call-closure of an entry point (a handler, a procedure, an API
  endpoint), plus every table it ends up reading or writing.

---

## How it works

1. **Extract.** Language-specific ingestors parse your code. A fast tree-sitter pass gets structure with
   no build step; a deeper pass adds real semantics where an analyzer exists (Roslyn for C#, Jedi for
   Python, an ANTLR grammar for PL/SQL). Auto-detects every supported language in one pass.
2. **Store.** Nodes and edges are written to FalkorDB with idempotent `MERGE`s. One graph per repo. Re-run
   it and only the delta changes.
3. **Serve.** An MCP server exposes curated, read-only graph tools to your agent; a web viewer exposes the
   same graph to you.

See `architecture.md` for the full diagram.

---

## What you can ask

- **"Where is `X` defined?"** — locate a symbol, get its id and file.
- **"Who calls `X`? What does `X` call?"** — inward/outward call graph, transitively.
- **"Does `A` reach `B`, and how?"** — the shortest path between two symbols, edge by edge.
- **"What breaks if I change `X`?"** — the change blast radius across dependency edges.
- **"What does this endpoint do, end to end?"** — the behavioral closure, including the tables it touches.
- **"List the public API / entry points."** — endpoints, handlers, exported surface.

Each answer comes back with node ids and file paths, and each edge with its confidence tag.

---

## Use cases

- **Understand an unfamiliar codebase** — get the map before reading files.
- **Change-impact / PR review** — see the blast radius of a diff before you ship it.
- **Trace a request through the tiers** — from a UI action to the controller to the database procedure.
- **Onboard an AI assistant to a big service** — one install, and every query is grounded.

---

## Where Auryn is different

- **A real graph database, not a file.** FalkorDB + Cypher means you can ask anything, and it scales to
  very large repos (proven on `dotnet/aspnetcore`).
- **Deep semantics, not just syntax.** Roslyn resolves interface calls to concrete implementations via
  dependency injection; Jedi infers Python types. Fewer "inferred" guesses, more "resolved" facts.
- **Cross-tier seams.** It models the edges that matter in real apps — UI → C# → PL/SQL → tables — that a
  single-language parser can't see.
- **Live database + source.** It can reflect a live Oracle schema *and* reconstruct the same schema from
  version-controlled DDL/PL-SQL — so you can compare "what's deployed" with "what's on this branch".

---

## Comparisons

- **vs. RAG / vector search** — vectors find text that *looks similar*; a graph finds what's *actually
  connected*. For "who calls this" or "what breaks", structure wins — and it's exact, not top-k.
- **vs. grep / built-in indexing** — grep matches strings; Auryn resolves symbols and relationships
  (a `resolved` call edge, not a name that happens to match).
- **vs. a static call-graph dump** — Auryn is a live, queryable, incrementally-updated database with
  cross-language edges and confidence tags, not a one-shot artifact.

---

## Honest limits (a "what it's not" section)

We'd rather tell you than have you find out:

- **Structural, not semantic-similarity.** No embeddings. It's the wrong tool for "find code that feels
  like this"; it's the right tool for "trace and cite this".
- **Line ranges not everywhere yet.** Some ingestors carry a file path and/or full source text but not a
  precise line span.
- **Two subgraphs, joined at seams.** C# and PL/SQL connect only through the modeled routing/UI seams —
  traversals won't invent edges across them elsewhere.
- **Validation logic isn't extracted yet.** "What conditions guard this path" is on the roadmap, not in
  the graph today.

---

## Open source & running it

Apache-2.0. Runs entirely on your machine by default (a bundled local database, no account, no
telemetry). Bring your own FalkorDB for a shared/team setup.
