# Auryn — live demo runbook

A story that **evolves**: one repo (honest gaps) → add its dependency (external symbols *heal*) →
add a second consumer (blast radius) → edit code (instant delta) → remove a repo → uninstall.
Every command is copy-paste, with **Say** (narration) and **See** (real output from a dry run).

Self-contained and repeatable: all state lives under an isolated `AURYN_HOME` + a throwaway
work dir, so it never touches your own code graph and resets in one line.

### The cast — three repositories (a real system, split up)

```
repos/acme-payments/       shared library   acme_payments.core: authorize · charge · refund
                                            acme_payments.ledger: record → os.getenv
repos/orders-service/      web service      app.api: POST /checkout, POST /promo  (imports acme_payments)
repos/invoicing-service/   web service      billing.monthly: run_monthly, reverse (imports acme_payments)
```

`charge` is the shared entrypoint every checkout path funnels through — so its blast radius spans
repositories. That's the whole point.

---

## 0. Setup (before the room is watching)

```bash
cd <this-repo>
export AURYN_HOME=/tmp/cg-pres         # isolate all state here
export WORK=/tmp/cg-pres-work
rm -rf "$AURYN_HOME" "$WORK" && mkdir -p "$WORK"

# stage editable git copies of the three repos
for r in acme-payments orders-service invoicing-service; do
  cp -r "demo/presentation/repos/$r" "$WORK/$r"
  git -C "$WORK/$r" init -q -b main && git -C "$WORK/$r" add -A
  git -C "$WORK/$r" -c user.email=demo@demo -c user.name=demo commit -q -m init
done
```

---

## 1. Install

```bash
uv run auryn install -y      # bundled FalkorDB (no Docker) + register the MCP server with agents
```

- **Say:** one command — it picks a backend (a *bundled* FalkorDB, nothing to run) and registers
  its MCP server with coding agents like Claude Code. Nothing to configure.
- *Note:* this adds a real user-scoped MCP entry to Claude Code (`claude mcp add -s user`); step 7
  removes it. Our step-4 helper drives the same tools directly, so no live agent session is needed.

> Prefer to keep the room's Claude Code config untouched? Skip `install`; everything below works
> against the isolated backend regardless. (For a fully hands-off backend, the runbook already
> points `AURYN_HOME` at a throwaway dir.)

---

## 2a. Ingest ONE repo — and see the honest gaps

```bash
uv run auryn ingest --graph-name acme dir "$WORK/orders-service" --deep off
uv run python demo/presentation/ask_agent.py acme
```

- **Say:** point it at the orders service and it builds the graph. Now ask an agent's favourite
  question — *what does the checkout endpoint actually do, end to end?*
- **See:** the endpoint traces into its handler, but the payment calls are **honest placeholders** —
  the tool won't pretend to know code that isn't in the graph:
  ```
  Q2. trace_entrypoint('POST /checkout')  — what does this endpoint do, end to end?
      POST /checkout  ↴
        → app.api.checkout             [orders-service] resolved
        → acme_payments.core.charge    [external]       unresolved · external
        → acme_payments.core.authorize [external]       unresolved · external
  ```
- **The hook:** "watch what happens when we give it the library those calls point to."

---

## 2b. Add the dependency — external symbols HEAL

```bash
uv run auryn ingest --graph-name acme dir "$WORK/acme-payments" --deep off
uv run python demo/presentation/ask_agent.py acme
```

- **Say:** ingest `acme-payments` into the **same** graph. No re-linking, no config — the very same
  question now resolves, and the trace **crosses the repo boundary**:
  ```
  Q2. trace_entrypoint('POST /checkout')  — end to end, ACROSS repos:
      POST /checkout  ↴
        → app.api.checkout             [orders-service] resolved
        → acme_payments.core.charge    [acme-payments]  resolved
        → acme_payments.ledger.record  [acme-payments]  resolved
        → os.getenv                    [external]       unresolved · stdlib
        → acme_payments.core.authorize [acme-payments]  resolved
  ```
- **The payoff:** one graph, two repos, and a checkout request traced all the way down to the
  ledger and the platform call it makes — order-independent (ingest either repo first).

---

## 2c. Add a second consumer — blast radius across the org

```bash
uv run auryn ingest --graph-name acme dir "$WORK/invoicing-service" --deep off
uv run python demo/presentation/ask_agent.py acme
```

- **Say:** invoicing also uses `charge`. Now ask the whole-system questions:
- **See:**
  ```
  Q3. who_calls('charge')  — impact / blast radius across every repo
        • orders-service     app.api.checkout
        • orders-service     app.api.apply_promo
        • invoicing-service  billing.monthly.run_monthly

  Q4. audit — every platform / builtin call the whole system makes
        • builtins.eval        [builtin]   ⚠ review
        • subprocess.run       [stdlib]
        • os.getenv            [stdlib]
  ```
- **Say:** "if we change `charge`'s signature, *these three call sites in two services* are the
  blast radius" — and "here's every risky/external call across the whole system, `eval` first."

---

## 3. Visual

```bash
uv run auryn view      # opens the web UI; pick the `acme` graph
```

- **Say:** the same thing, visually — expand `charge` to see callers light up in **both** services;
  the cross-repo `CALLS`/`IMPORTS` edges are real, placeholders (`os.getenv`) show `resolved=false`.

---

## 4. Agent interaction

Already shown via `ask_agent.py` (it drives the real MCP tools an agent uses). Now flip it around —
**how well did the agent use the graph?**

```bash
uv run auryn stats
```

- **See:** calls by operation, error rate, result sizes — visibility into agent usage:
  ```
  Code graph accesses: N   (N read, 0 write)
    operation          op    calls  err  avg ms  avg result
    trace_entrypoint   read     …     0      …          …
    who_calls          read     …     0      …          …
    locate             read     …     0      …          …
  ```

---

## 5. Delta sync

```bash
cat >> "$WORK/orders-service/app/api.py" <<'EOF'


@app.post("/reorder")
def reorder(last_total):
    return charge(last_total)          # cross-repo → acme-payments
EOF

uv run auryn sync "$WORK/orders-service"
uv run python demo/presentation/ask_agent.py acme        # who_calls charge now includes app.api.reorder
```

- **Say:** edit the code — a new `/reorder` endpoint that charges again. `sync` uses git to see
  exactly what changed and the graph to find the blast radius, so it re-parses only that file.
- **See:** a delta (`nodes +8/-2, edges +7/-6`), and `who_calls(charge)` now includes
  **`app.api.reorder`** — the new caller, already resolved across the repo boundary. On a large
  repo this is the ~150× speed-up over re-ingesting.
- *(Tip: call a symbol imported at module level — the fast engine binds those; a function-local
  `import` needs the deep pass.)*

---

## 6. Remove a repo

```bash
uv run auryn forget "$WORK/invoicing-service" --dry-run     # preview
uv run auryn forget "$WORK/invoicing-service" --yes
```

- **Say:** the counter to ingest — it removes just that repo's subgraph from the shared graph. Your
  files are never touched.
- **See:** `Will delete project 'invoicing-service' in graph 'acme': 7 nodes`, and `who_calls(charge)`
  drops back to the two orders-service callers — the blast radius shrinks in real time.
- **Optional (the robustness beat):** forget the *shared library* instead —
  `auryn forget "$WORK/acme-payments" --yes` — and every service's calls into it **demote back to
  placeholders** rather than dangle. Re-ingest and they heal again. Removal is safe and reversible.

---

## 7. Uninstall

```bash
uv run auryn uninstall --yes        # stop the server, deregister from agents
# add --purge to also delete every graph + config
```

- **Say:** one command reverses install — stops the bundled server, removes the MCP registration
  from every agent. `--purge` wipes graphs and config too. Clean machine.

---

## 8. If you have time (pick one or two)

- **Zero-setup opener:** `uv run auryn quickstart "$WORK/orders-service"` — ingest + open the
  viewer against an *embedded* FalkorDB. No install, no server, no Docker.
- **Broken-code resilience:** introduce a syntax error, `sync`, and the graph keeps the last good
  version (reports `broken_files`) instead of dropping symbols.
- **Polyglot:** the same story holds for 11 languages — one graph, cross-repo edges; code→DB next.
- **Freshness numbers:** incremental resync is ~150× faster than a full re-ingest on Django.

---

## Reset (between runs)

```bash
uv run auryn uninstall --purge --yes 2>/dev/null || true
rm -rf "$AURYN_HOME" "$WORK"
```
