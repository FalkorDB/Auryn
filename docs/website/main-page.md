# Auryn — homepage content

*Copy and section structure for the landing page. Mirrors Graphify's hero-first, developer-first
layout. Bracketed notes are for the designer, not body copy.*

---

## Nav

`Product` · `Developers` · `Docs` · `GitHub` — with a primary button **`Install`** (or **`Get started`**).

---

## Hero

**Headline:**
> Your codebase as a graph your AI can trace — not grep.

**Subheadline:**
> Auryn turns any repository into a queryable knowledge graph and hands it to your coding
> assistant through MCP. It answers "who calls this", "what breaks if I change it", and "what does this
> endpoint actually do" by *following real edges and citing them* — no embeddings, no guessing.

**Primary CTA:** `auryn install`  → **Get started**
**Secondary CTA:** `View on GitHub` · `Read the docs`

[Design: hero visual = the graph explorer screenshot (dark canvas, glowing nodes, module clusters).
Consider a subtle live/animated force-directed graph in the same palette.]

**Under the hero (trust strip):**
`Open source` · `Runs on your machine` · `Real graph database, real Cypher` · `7 languages` · `Works with Claude Code`

---

## Section 1 — The problem (short)

**Heading:** Grep doesn't understand your code. A graph does.

Your assistant reconstructs structure by reading and grepping files — slow, token-hungry, and easy to
get wrong on a large or polyglot codebase. Auryn gives it a **map**: one query returns the exact
structural answer with ids and file paths, without pulling whole files into context.

---

## Section 2 — How it works (3 steps)

**Heading:** Install once. Your agent does the rest.

1. **Install & register** — `auryn install` sets up a backend (a bundled local database, or your
   own) and registers Auryn with your coding agent, user-scoped. Nothing is written into your repo.
2. **Index on demand** — the first time you ask a structural question, the agent builds the graph for
   that repo (`index_repository`). One-time cost; every query after is cheap.
3. **Trace & cite** — the agent uses graph tools — *locate → who-calls → impact → path* — and answers
   with evidence: node ids, file paths, and a confidence tag on every edge.

[Design: three cards, each with a small graph-motif icon. Show a real command / tool name in mono.]

---

## Section 3 — Feature grid

**Heading:** Built to be precise, honest, and fast.

- **Structure, not similarity.** Follows real call/import/dependency edges. Results are exact and
  complete-by-construction — not the top-k nearest vectors.
- **Every edge is sourced.** Relations are tagged `resolved`, `inferred`, or `unresolved`; types as
  `annotated`, `inferred`, or `unresolved`. The agent trusts the strong ones and tells you when it's guessing.
- **7 languages, one graph.** Python, TypeScript/JavaScript, C#, Java, C, PL/SQL, and SQL — auto-detected
  and ingested in a single pass.
- **Deep where it counts.** Real semantic analysis — Roslyn (C#, incl. DI resolution), Jedi (Python),
  an ANTLR PL/SQL grammar — not just syntax.
- **Cross-tier tracing.** Follows a UI event into the controller and on into the database procedure it
  calls: **UI → C# → PL/SQL → tables**, one unbroken trace.
- **A real graph database.** FalkorDB under the hood — run arbitrary read-only Cypher, scale to hundreds
  of thousands of nodes.
- **Runs on your machine.** A bundled, serverless database starts on demand — no server, no Docker, no
  account. Or point at your own FalkorDB.
- **Always current, cheaply.** Git-tracked: a per-blob parse cache means a big changeset re-parses a
  handful of files, not the whole repo.

---

## Section 4 — See it

**Heading:** Explore the graph, or let your agent query it.

A built-in web explorer renders the whole graph: colour by subsystem (automatic community detection),
trace the shortest path between any two symbols, and inspect the schema. The same read layer powers the
agent's tools — so what you see is exactly what it queries.

[Design: 2–3 of the attached viewer screenshots — overview, community coloring, path-finder.]

---

## Section 5 — Who it's for

- **Individual developers** — stop re-explaining your codebase to your assistant.
- **Platform / DX teams** — a shared, honest map of a large service.
- **Polyglot & enterprise codebases** — especially .NET + Oracle/PL-SQL, where the important edges cross
  language and tier boundaries.

---

## Final CTA

**Heading:** Give your assistant a map.
`auryn install`  → **Get started** · `Star on GitHub`

**Footer badges:** Open source (Apache-2.0) · On-device · No telemetry
