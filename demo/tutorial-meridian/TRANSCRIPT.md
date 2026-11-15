# Meridian architecture review — real Auryn session transcript

This is a **verbatim capture of an actual conversation**. The **user** turns were sent by a person
doing an architecture review; the **agent** is a Claude coding agent with the **auryn skill**
installed, answering by querying the graph. It began with **no pre-built graph** — the agent indexed
the four repos itself in turn 1. The agent reached the auryn MCP tools through the in-process
driver (`ask.py`), i.e. the same `auryn_status` / `index_repository` / `locate` / `callees` /
`who_calls` / `trace_entrypoint` / `run_cypher` tools the MCP server exposes; every result below is
what the tools actually returned. The `→` lines are the agent's own record of the tool calls it made.

The system under review: four repos under `/tmp/cg-live-work/` — `web-portal` (React/TS),
`api-gateway` (C#/ASP.NET), `platform-core` (Python/FastAPI), `platform-db` (Oracle PL/SQL).

---

### 👤 User

> I've just installed Auryn. I'm doing an architecture review of our "Meridian" platform — the
> four repos under /tmp/cg-live-work. Can you get yourself set up and then tell me what this system
> is actually made of?

### 🤖 Agent

Setup done and the graph answers the question. Here's what I ran and found.

```
→ auryn_status()  → graph "meridian" empty; 4 repos on disk, nothing indexed yet
→ index_repository(web-portal) → TypeScript: 7 files, 39 nodes, 5 /api/* callers
→ index_repository(api-gateway) → C#: 10 files, 87 nodes, 6 controllers/services
→ index_repository(platform-core) → Python: 9 files, 68 nodes, 5 FastAPI endpoints
→ index_repository(platform-db) → PL/SQL: 1 file, 30 nodes, 5 tables (seam now 6/6 resolved)
→ run_cypher(labels+edges) → 225 nodes, cross-repo HTTP seam wired (10 CALLS_API, 16 EXPOSES_ENDPOINT)
→ run_cypher(HTTP seam map) → every /api/* route joins a TS caller → C# controller → Python endpoint
→ run_cypher(cross-project edges + tables) → confirmed layer boundaries + a DB-wiring gap
→ trace_entrypoint(placeOrder) → one click traced React → C# → HTTP → Python → data layer
```

## What "Meridian" actually is

A **4-tier order/billing platform**, one language per tier, stitched end-to-end through an HTTP seam the graph resolved automatically:

| Tier | Repo | Stack | Size | Role |
|---|---|---|---|---|
| Frontend | `web-portal` | TypeScript / React + axios | 7 files, 39 nodes | UI components + one API client (`src/api/client.ts`) |
| Gateway | `api-gateway` | C# / ASP.NET | 10 files, 87 nodes | Controllers expose `/api/*`, services proxy to core |
| Core | `platform-core` | Python / FastAPI | 9 files, 68 nodes | Business logic + endpoints + data-access layer |
| Database | `platform-db` | Oracle PL/SQL | 1 file, 30 nodes | Schema: 5 tables, 19 columns, 5 constraints |

**The request path (fully resolved, not inferred).** React → gateway → core is a clean 3-hop chain per feature. Example, from the `placeOrder` trace: `client.ts:placeOrder` → `POST /api/orders` → C# `OrdersController.PlaceOrder` → `OrderService.Checkout` → `POST /charge` → Python `charge_endpoint` → `billing.charge` → `ledger.record` + `accounts.get_limit`/`get_account` + `audit.write` → `db.execute`/`query`. Five features are wired identically end to end: **orders, refunds, promotions, charge, daily-report** (10 `CALLS_API` edges, all seam endpoints `resolved:true`).

- **Frontend calls** (`web-portal`): `/api/orders`, `/api/orders/{id}`, `/api/promotions`, `/api/refunds`, `/api/reports/daily`
- **Gateway → core calls** (`api-gateway` services): `/charge`, `/orders/{id}`, `/promo`, `/refund`, `/reports/daily`
- **Core domain modules** (`platform-core`): `billing`, `ledger`, `accounts`, `audit`, `reports`, `orders`, `db`
- **DB tables** (`platform-db`): `ACCOUNTS`, `ORDERS`, `LEDGER_ENTRIES`, `TRANSACTIONS`, `AUDIT_LOG`

## Two findings worth flagging

1. **The database tier is orphaned in the graph.** The PL/SQL schema (`ACCOUNTS`, `ORDERS`, `LEDGER_ENTRIES`, `TRANSACTIONS`, `AUDIT_LOG`) conceptually mirrors what the Python core persists, but there is **no static edge** connecting `platform-core` to `platform-db`. Python's data layer (`meridian.db.connect`) calls `sqlite3.connect` (`extern://python/sqlite3::connect`), not the Oracle package. So the Oracle schema is a **parallel data model, not the store the running app talks to** — either an intended migration target, a prod/dev mismatch, or dead schema. Worth confirming which.

2. **Two risky calls the graph surfaced in the core.** `meridian.billing.apply_promo` calls `builtins::eval` (evaluating a promo code — injection risk), and `meridian.reports.daily_report` calls `subprocess::run` (shelling out for report generation). Both are on live request paths (`/promo`, `/reports/daily`) and are good candidates for the security portion of your review.

The graph is persisted (`meridian`) with git-commit resync hooks installed on all four repos, so it stays current as you work. Ask me anything else structural — blast radius of a change, who-calls, or a path between any two symbols — and I'll query it rather than grep.

---

### 👤 User

> Good overview. Billing is obviously the money path, and I want to plan a change to it — but first
> show me precisely what `charge` calls downstream, so I understand its full footprint before I touch it.

### 🤖 Agent

`callees(charge, max_depth=6)` → full downstream tree, all internal edges `resolved`. Here is the precise footprint.

```
→ callees(meridian.billing.charge, depth=6) → 8 internal functions + 4 external leaves, no truncation

charge(account_id, amount, currency)            [billing]
├─ authorize(account_id, amount)                [billing]   ← credit check
│  ├─ get_limit(account_id)                     [accounts]
│  │  └─ get_account(account_id)                [accounts]
│  │     └─ db.query(sql, *params)              [db]  reads
│  │        ├─ db.connect()                     [db]
│  │        │  ├─ sqlite3.connect   (extern, stdlib)
│  │        │  └─ os.getenv         (extern, stdlib)
│  │        ├─ builtins.dict        (extern)
│  │        └─ builtins.zip         (extern)
│  └─ audit.write("...", "...")                 [audit]
│     └─ db.execute(sql, *params)               [db]  writes
│        └─ db.connect() → sqlite3.connect / os.getenv
├─ ledger.record(account_id, amount, txn)       [ledger]    ← writes the money row
│  ├─ db.execute(sql, *params)                  [db]  writes
│  │  └─ db.connect() → sqlite3.connect / os.getenv
│  └─ os.getenv                    (extern)
└─ audit.write("charge", "...")                 [audit]
   └─ db.execute(sql, *params) → db.connect → sqlite3.connect / os.getenv
```

## Footprint at a glance

`charge` makes **3 direct calls** — `authorize` (billing), `ledger.record` (ledger), `audit.write` (audit) — and pulls in **8 first-party functions total** across 5 modules (`billing`, `accounts`, `ledger`, `audit`, `db`), bottoming out in **4 external leaves** (`sqlite3.connect`, `os.getenv`, `builtins.dict`, `builtins.zip`). Every first-party edge is `resolution:resolved` — high confidence, nothing inferred, result not truncated.

**Three things that matter for planning your change:**

1. **Everything converges on `meridian.db`.** All three branches funnel into `db.execute` (writes: ledger row + 2 audit rows) or `db.query` (reads: account limit), and both go through the single `db.connect()` → `sqlite3.connect`. `charge` touches the DB at least 4 times per call. If your change is about the DB tier, `db.connect/execute/query` are the shared choke point — and they're used by far more than just `charge` (high blast radius; ask me for `who_calls` on them before touching).

2. **The money-specific logic is small and contained.** The parts unique to charging — `authorize` (limit check), `ledger.record` (the actual ledger write), and the audit trail — live in `billing`/`accounts`/`ledger`/`audit`. That's the safe surface to modify if the change is behavioral (e.g., adding currency handling or a stronger auth check) rather than infrastructural.

3. **`charge` itself is clean** (no `eval`/`subprocess` in this subtree — those smells were in `apply_promo` and `daily_report`, which are *not* reachable from `charge`). Its own source also notes it's a shared funnel called by more than one service — so before you touch its signature or return contract, check upstream: it's invoked via `/charge` by the C# `OrderService.Checkout`, and `authorize` is also called independently. I can run `who_calls(charge)` to give you the exact inbound blast radius when you're ready to scope the change.

---

### 👤 User

> Yes — do that. We're going to change `charge`'s signature, so I need the full inbound blast radius:
> everything that reaches it across ALL the repos, including the public `/api/*` routes and the UI
> actions behind them, not just the direct callers in platform-core.

### 🤖 Agent

`run_cypher(two call-site sources)` → the two internal callers pass `charge` differently. Here is the complete inbound blast radius.

```
→ who_calls(charge, depth=6)         → 2 direct core callers: charge_endpoint AND apply_promo (+promo_endpoint via apply_promo)
→ run_cypher(gateway tier)           → /charge and /promo both reach charge; 2 C# services, 2 controllers
→ run_cypher(UI tier)                → 2 web-portal flows: CheckoutButton and PromoField
→ run_cypher(call-site sources)      → charge_endpoint passes 3 args; apply_promo passes only 2 (currency defaulted)
```

## `charge` has TWO inbound flows across all four repos — not one

Everything that reaches `charge(account_id, amount, currency="USD")` traces back to two distinct UI actions:

**Flow 1 — Checkout / Orders**
```
CheckoutButton.tsx  →  client.ts:placeOrder  ──POST /api/orders──▶  OrdersController.PlaceOrder
   →  OrderService.Checkout  ──POST /charge──▶  charge_endpoint  →  charge
```

**Flow 2 — Promotions**
```
PromoField.tsx  →  client.ts:applyPromo  ──POST /api/promotions──▶  PromotionsController.ApplyPromo
   →  BillingService.ApplyPromo  ──POST /promo──▶  promo_endpoint  →  apply_promo  →  charge
```

## The complete call-site list you must reconcile on a signature change

| Tier | Repo | Node (must be updated / re-tested) |
|---|---|---|
| **Core, direct call** | platform-core | `meridian.api.charge_endpoint` — calls `billing.charge(account_id, amount, currency)` |
| **Core, direct call** | platform-core | `meridian.billing.apply_promo` — calls `charge(0, amount * factor)` |
| Core, indirect | platform-core | `meridian.api.promo_endpoint` → `apply_promo` |
| Seam (HTTP body builders) | api-gateway | `OrderService.Checkout` (→ `POST /charge`), `BillingService.ApplyPromo` (→ `POST /promo`) |
| Gateway controllers | api-gateway | `OrdersController.PlaceOrder`, `PromotionsController.ApplyPromo` |
| Public routes | api-gateway | `POST /api/orders`, `POST /api/promotions` |
| UI API client | web-portal | `client.ts:placeOrder`, `client.ts:applyPromo` |
| UI components | web-portal | `CheckoutButton.tsx`, `PromoField.tsx` |

**Only two call sites bind directly to the Python signature** (`charge_endpoint` and `apply_promo`); the other tiers bind to the *HTTP contract* (`/charge` request body), so a signature change there only bites if it changes the wire payload the C# services in `api-gateway` build.

## The landmine to watch

The two direct callers pass `charge` **inconsistently**, and the graph shows exactly why that's dangerous for a signature edit:

- `charge_endpoint` → `billing.charge(account_id, amount, currency)` — passes all three, positionally.
- `apply_promo` → `charge(0, amount * factor)` — passes **only two** positional args; `account_id` is hardcoded to `0` and `currency` relies on the `="USD"` default.

So if your change adds a required parameter, reorders them, or removes the `currency` default, the **`apply_promo` call site breaks or silently misbehaves** (the promo flow charges account `0`), while `charge_endpoint` might look fine in review. That's the site to change most carefully, and the promo flow (PromoField → /api/promotions → /promo) is the end-to-end path to regression-test.

One more thing the graph surfaced while tracing this: `apply_promo` runs `eval(code)` on the caller-supplied promo code — an RCE risk sitting directly on the money path into `charge`. Worth folding into the same change since you're already touching this code.

That completes the setup and both follow-ups: the graph (`meridian`, all four repos, resync hooks installed) is ready for the rest of your review. Ask for a `who_calls`/`callees`/`find_path` on any node id above and I'll pull it from the graph.

---

### 👤 User

> That eval is now my priority. Before I touch it, prove it concretely: is untrusted, user-supplied
> input actually able to reach `eval` from a public endpoint? Trace the promotions endpoint end to
> end and show me the sink.

### 🤖 Agent

Proven. `→ trace_entrypoint(POST:/api/promotions)` lists `extern://python/builtins::eval` inside the endpoint's reachable closure, and the per-hop source shows the same `code` string flows through every tier untouched. This is a confirmed, reachable RCE from a public endpoint.

## The taint path: raw text box → `eval`, no validation anywhere

`code` originates as untrusted keystrokes and reaches `eval` unmodified across 3 repos and 2 HTTP hops:

```
[1] PromoField.tsx        <input value={code} onChange={e => setCode(e.target.value)} />
                          → applyPromo(code, amount)                        ← untrusted SOURCE (free-text UI field)
[2] client.ts:applyPromo  api.post("/api/promotions", { code, amount })     ← sent verbatim in body
        ── HTTP POST /api/promotions ──▶
[3] PromotionsController   [HttpPost("api/promotions")] ApplyPromo(PromoRequest req)
                          → _billing.ApplyPromo(req)                         ← no validation
[4] BillingService        _http.PostAsJsonAsync("/promo", req)              ← forwarded verbatim
        ── HTTP POST /promo ──▶
[5] promo_endpoint(code)  billing.apply_promo(code, amount)                  ← passed through
[6] apply_promo(code)     factor = eval(code)                               ← *** SINK: eval on caller input ***
```

## Why this is conclusive

- **The sink is in the closure.** `trace_entrypoint` on the public endpoint `extern://http/POST:/api/promotions` returns `extern://python/builtins::eval` among its reachable functions — the graph itself asserts the public route can reach `eval`.
- **The taint is unbroken.** Every hop's source (above) passes `code`/`req` straight through: no allow-list, no regex, no `int()`/`Decimal` parse, no schema validation. `code` is a `string` from the `<input>` all the way to `eval(code)`. C# `PromoRequest` is a pass-through DTO; the two service hops just re-serialize the same body.
- **High confidence.** Every edge on this path is `resolution: resolved` (first-party) or an explicit `external` builtin (`eval`); nothing is inferred, and the trace was not truncated.

## Impact

Anyone who can POST to the public `/api/promotions` route (the promo field on the checkout page) controls the string handed to Python `eval` — arbitrary code execution inside `platform-core`. And because `apply_promo` then calls `charge(0, amount * factor)`, the same request continues into the money path (ledger write + audit). So this single field is both an RCE and a billing-integrity hole.

**Fix at the sink** (`meridian.billing.apply_promo`, platform-core): replace `factor = eval(code)` with a lookup of `code` against a known promo table or a strict decimal parse. Optionally add input validation at the public boundary (`PromoRequest` in api-gateway) as defense-in-depth, but the sink fix is the one that closes the hole.

That addresses the coordinator's request: the eval sink is concretely reachable from the public promotions endpoint, with the full source-to-sink taint chain shown above. The `meridian` graph remains loaded for any follow-up.

---

### 👤 User

> Clear enough — close it at the sink. Change `apply_promo` in platform-core to resolve the promo
> code against a fixed lookup table (default to no discount for unknown codes) instead of calling
> `eval`, and commit the change.

### 🤖 Agent

The fix is done, committed, and verified at both the code and graph level.

```
→ Edit(billing.py)            → apply_promo now resolves code via PROMO_FACTORS lookup, no eval
→ Bash(python verify)         → no eval in source/AST; known codes discount, unknown+RCE payload → no discount
→ git commit                  → ca63adf on branch fix/apply-promo-eval-rce
→ index_repository(resync)    → 1 file changed, edges -1 (the CALLS→eval edge removed)
→ run_cypher(eval edge)       → 0 rows — no caller reaches eval
→ run_cypher(eval node)       → gone — the extern eval node was GC'd (no inbound edges left)
→ trace_entrypoint(/api/promotions) → eval NOT in closure; charge still in closure (promo still charges)
```

## What changed

In `/tmp/cg-live-work/platform-core/meridian/billing.py`, `apply_promo` no longer calls `eval(code)`. It now looks the code up in a fixed table and defaults unknown codes to no discount:

```python
PROMO_FACTORS: dict[str, float] = {"WELCOME10": 0.90, "SAVE20": 0.80, "HALFOFF": 0.50}

def apply_promo(code: str, amount: float) -> str:
    factor = PROMO_FACTORS.get(code, 1.0)     # unknown code -> no discount
    return charge(0, amount * factor)
```

## Proof it closed the hole

- **Behavior:** `HALFOFF`→50.0, `WELCOME10`→90.0, unknown `BOGUS`→100.0 (no discount), and the prior attack payload `__import__("os").system(...)`→100.0 with **no execution**. `eval` appears nowhere in the source or the parsed AST.
- **Graph:** after resync the ingestor dropped exactly one edge — `apply_promo ─CALLS→ builtins::eval`. That was the only inbound edge to the `eval` node, so the node itself was garbage-collected. A fresh `trace_entrypoint` on the public `POST /api/promotions` endpoint no longer contains `eval` in its closure. The RCE taint path from the public endpoint to the sink is gone.
- **No collateral damage:** the promotions flow still reaches `charge` (the trace confirms `billing.charge` remains in the closure), so promos still apply and charge — they just resolve through the lookup table instead of `eval`.

## Committed

Branch `fix/apply-promo-eval-rce`, commit `ca63adf` (I branched off `main` rather than committing to the default branch directly; no remote, so nothing was pushed). The change is one file, +14/-4.

Note: the graph is now updated only for the working tree via my manual re-index. The auto-resync hook is keyed to commits on the tracked branch; since I committed on a side branch, merging `fix/apply-promo-eval-rce` into `main` is what you'll want next if this is going to production. Want me to open a PR or trace any of the other findings (the `subprocess.run` in `daily_report`, or the orphaned PL/SQL schema tier)?

---

### 👤 User

> Good call branching it. Go ahead and merge `fix/apply-promo-eval-rce` into `main` so the
> commit-hook keeps the graph current, then give me a final confirmation straight from the graph:
> does it now reflect the fix, and is there definitely no path left from any public endpoint to `eval`?

### 🤖 Agent

Merged and confirmed. Both answers come straight from the graph.

```
→ git merge --ff-only     → main fast-forwarded 8cb2f33 → ca63adf (the fix is now on main)
→ index_repository        → graph already at this tree (0 files parsed, 0 changed) — it was current from the prior resync
→ run_cypher(apply_promo.source) → graph shows the new PROMO_FACTORS.get(code, 1.0) body, no eval
→ run_cypher(eval nodes)  → 0 — no node named "eval" / "::eval" exists anywhere in the graph
→ run_cypher(public endpoint → eval) → paths_to_eval = 0
```

## Final confirmation

**1. Does the graph reflect the fix?** Yes. `main` is now at `ca63adf`, and the graph's stored source for `meridian.billing.apply_promo` is the new implementation: `factor = PROMO_FACTORS.get(code, 1.0)` returning `charge(0, amount * factor)`. No `eval` in the body. (The re-index reported 0 changed files because the graph was already synced to this exact tree from the earlier working-tree resync — merging to `main` didn't change the content, only the branch pointer.)

**2. Is there definitely no path left from any public endpoint to `eval`?** Confirmed, two ways:
- The `eval` node no longer exists in the graph at all — it was garbage-collected once its only inbound `CALLS` edge (from `apply_promo`) was removed. Query for any node named `eval` returns **0 rows**.
- The exhaustive reachability query — every `APIEndpoint` in the graph, traversing up to 12 hops across all "call" edge types (`EXPOSES_ENDPOINT`, `CALLS_API`, `CALLS`) — returns **`paths_to_eval = 0`**.

The RCE sink is gone from the codebase and from the graph, no public endpoint can reach it, and `main` carries the fix so the commit-hook will keep the graph current from here. The `meridian` graph remains loaded if you want to move on to the other findings (the `subprocess.run` in `daily_report`, or the orphaned PL/SQL schema tier).

---

*End of session. The graph was built from empty in turn 1 and stayed current through the fix — the
agent answered every question by querying it, and verified its own change against it.*
