# Multi-repo code graph demo

Why put several repositories into **one** code graph? Because real systems span repos — a service,
a shared library, another service — and the questions that matter most (*what breaks if I change
this? what depends on that? what touches the network / calls `eval`?*) cross those boundaries.

This demo makes the difference concrete with three tiny repositories:

| repo | role | references |
|------|------|-----------|
| `repos/billing`   | shared library | defines `billing.core.charge` / `refund` |
| `repos/orders`    | web service    | imports billing, calls `charge`; uses `os.getenv`, `eval` |
| `repos/reporting` | service        | imports billing, calls `charge`/`refund`; uses `subprocess` |

## Run

```bash
uv run python demo/multi-repo/run_demo.py
```

Needs a FalkorDB — a local one on `:6379` (or `auryn quickstart`), otherwise the script falls
back to the in-process embedded build (`pip install 'auryn-core[embedded]'`).

## What it shows

**A — a graph per repo (the default).** Ingested on its own, the `orders` graph *cannot* see
billing: the call `place_order → charge` is an honest but **unresolved placeholder**
(`extern://python/billing.core::charge`, `resolved=false`). "Who else calls `billing.charge`?" is
unanswerable — the other repos aren't in the graph.

**B — all three in one shared graph** (`ingest --graph-name system …`). The *same* call now
**heals** into a real cross-repo edge to `billing`'s actual definition, and the system is queryable
as a whole:

- **Q1 — impact / blast radius:** every caller of `billing.core.charge`, *across all repos*
  (`orders.place_order`, `orders.apply_promo`, `reporting.daily_report`).
- **Q2 — dependency mapping:** what `orders` depends on from other repositories.
- **Q3 — security / portability audit:** every platform/builtin call the whole system makes,
  grouped by `origin` (`eval` → `builtin`, flagged; `os.getenv` / `subprocess.run` → `stdlib`).

Nothing about the code changes between A and B — only whether the repos share a graph. That is the
multi-repo advantage: locally-correct per-repo graphs are blind across boundaries; one shared graph
lets you ask impact, dependency, and audit questions of the system as a whole.

## Explore it visually

After running, open the viewer and pick the `demo_system` graph to browse the cross-repo
`CALLS` / `IMPORTS` edges (placeholders show `resolved=false`):

```bash
uv run auryn view   # serves a web UI over every graph on the instance; select `demo_system`
```

## How it works

Cross-repo resolution is [ADR 0004](../../docs/adr/0004-cross-repo-symbol-resolution.md): an
unresolved reference becomes a first-class placeholder node keyed by a project-independent
`linkage_key`; ingesting the defining repo into the same graph reconciles the placeholder onto the
real definition (order-independent, index-backed, no graph-wide scans). It works for every language
the ingestor supports — this demo uses Python for brevity.
