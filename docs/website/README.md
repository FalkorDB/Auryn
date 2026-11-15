# Auryn — website content package

This folder is a **ready-to-paste content brief for [claude.ai/design](https://claude.ai)** to design
the Auryn marketing site. It's modeled on the structure of [graphify.com](https://graphify.com)
(hero → Product → Developers), tailored to what Auryn actually does.

## What's here

| File | Use it for |
|---|---|
| `main-page.md` | Homepage: hero, positioning, feature sections, CTAs, social proof |
| `product.md` | "Product" page: what it is, how it works, use cases, comparisons, honest limits |
| `developers.md` | "Developers" page: install, **get-started tutorial**, commands, MCP tools, language support, agent integration |
| `architecture.md` | Technical architecture (for a diagram/section, and to keep copy accurate) |
| `screenshots.md` | Screenshot manifest — what each attached image shows and where to use it |

## Design direction (hand this to the designer)

- **Aesthetic:** dark, technical, "network/graph" motif. The product's own viewer sets the tone —
  near-black canvas (`#0d1117`-ish), soft-glow nodes in a calm categorical palette (coral, amber,
  green, blue, lilac), thin grey edges, monospace for identifiers/commands. See the attached
  screenshots and `screenshots.md`.
- **Voice:** developer-first, precise, confident, honest. Short declarative claims backed by concrete
  detail. No hype adjectives; let the capability speak. Graphify's line *"a graph it can trace and
  cite, not grep"* is the register to match.
- **Hero visual:** the graph explorer screenshot (or an animated force-directed graph in the same
  palette) is the money shot — it instantly says "your codebase, as a graph."
- **Signature idea to feature:** *the graph is the map — used to find, not to search.* Structure over
  similarity. Answers carry evidence (node ids, file paths, confidence tags).

## Naming note

Product name throughout is **Auryn**; the CLI/distribution is `auryn` (e.g. `auryn install`). Keep
that consistent on the site.
