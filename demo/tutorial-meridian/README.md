# Tutorial: architecture review of a polyglot platform

An Auryn tutorial built around one story — a **staff architect auditing the Meridian payments
platform** — asked as a gradual conversation with an AI agent that answers through the `auryn`
MCP tools. It captures both the value and the agent's interaction with the tool.

- **`TRANSCRIPT.md`** — the deliverable: a **verbatim capture of a real session**. A user drives an
  architecture review; a Claude agent with the auryn skill answers by querying the graph.
  Starts with **no graph** (the agent indexes the four repos itself in turn 1), then escalates
  orient → understand → assess risk → act (fix the `eval` RCE + resync + re-verify).
- **`repos/`** — the system under review: `web-portal` (React/TS), `api-gateway` (C#),
  `platform-core` (Python/FastAPI), `platform-db` (SQL). Coherent so cross-repo + HTTP-seam edges
  resolve. `platform-core/meridian/billing.py` ships the `eval` vuln the audit finds.
- **`setup.sh`** — stage git copies + ingest all four repos into one graph (isolated
  `AURYN_HOME`).
- **`ask.py`** — drives the real MCP tools in-process; the transcript was captured with it.

## Run it

```bash
bash demo/tutorial-meridian/setup.sh
export AURYN_HOME=/tmp/cg-tutorial
uv run python demo/tutorial-meridian/ask.py trace_entrypoint '{"entry_id":"extern://http/POST:/api/orders"}'
```

## Spine of the demo

A **Checkout** click in `web-portal` → `POST /api/orders` (C# gateway) → `POST /charge`
(Python platform, via the HTTP seam) → `billing.charge` → ledger/audit/db. `charge` is the shared
funnel every money path converges on — so it's where blast-radius and cross-repo questions get
interesting.

> The tutorial exercises three small, **test-backed** ingestor improvements the polyglot chain
> depends on: the HTTP stitch resolves calls onto Python (`Function`-exposed) endpoints, not just C#
> Methods; `trace_entrypoint` crosses the HTTP seam end to end; and `run_cypher` returns
> tabular/aggregation results, not only graph entities.
