# Auryn — screenshot manifest

Real screenshots of the Auryn **web viewer**, captured against a live graph of Auryn's own
codebase (7-language ingestor, 132 nodes / 130 edges shown). They're attached to the delivery message;
the live viewer was also running at `http://127.0.0.1:8090` if you want to grab more states yourself
(node-detail panel, path-finder trace, the Query tab).

These set the visual tone for the site — dark, calm, graph-centric. Hand them to the designer alongside
`main-page.md` (§4 "See it") and the hero.

| # | Shot | Shows | Use on the site |
|---|---|---|---|
| 1 | **Graph overview** | Force-directed graph on a near-black canvas; nodes glow in a categorical palette (coral Modules, amber Functions, green Packages, blue Projects); labeled module clusters (`core`, `cli`, `mcp`, `packages`, `python`, `csharp`, `typescript`, `java`, `plsql`). Left sidebar: graph picker, search, Display/Path/Query/Schema/Sources tabs, a Categories legend with live counts. | **Hero visual.** The single best "your codebase as a graph" image. |
| 2 | **Colour by community** | Same graph recolored by **auto-detected community** (blues/greens/purples/lilac). Sidebar shows a "COMMUNITIES · 95 IN VIEW" legend with named clusters and counts (`ingestors/csharp` 32, `auryn/core`, `python`…). | §"See it" — the "it finds your subsystems automatically" story. |
| 3 | **Schema / relationships** | The Schema tab: relationship types with live counts — `HAS_PARAMETER 1.9k`, `CALLS 885`, `HAS_ATTRIBUTE 675`, `HAS_FUNCTION 604`, `HAS_METHOD 238`, `EXTENDS 2`… over the same graph. | §"Structure, not similarity" / Product page — proof it's a real, typed graph model, not embeddings. |

## Palette pulled from the product (for the site)

- Canvas / background: near-black, `#0d1117`-ish.
- Nodes (categorical): coral/pink `~#e0607e`, amber `~#e0a45c`, green `~#8ec07c`, blue/cyan `~#7ab0d6`,
  lilac `~#b39ddb`, muted grey for de-emphasized.
- Edges: thin, low-opacity grey-blue.
- Text: off-white `~#c7cedb`; identifiers/commands in a monospace face.
- Accent (buttons, active tab): a warm coral or the product's blue — pick one and commit.

## Suggested additional shots (optional, grab from the live viewer)

- **Node detail** — click a node → its properties, source, and 1-hop neighbours. Great for the
  "answers with evidence" section.
- **Path-finder** — the Path tab tracing the shortest chain between two symbols (highlighted). Great for
  "trace and cite".
