# ADR 0006 — HTTP call ↔ endpoint seam: a language-agnostic route-keyed stitch

Status: **Proposed.** Date: 2026-09-25.

Builds on ADR 0004 (cross-repo symbol resolution: external references as upgradable nodes),
ADR 0003 (multi-repo shared graph), and the cross-ingestor schema contract.

## Context

A large share of "who calls what" in a modern system crosses an **HTTP boundary**, and that
boundary is invisible to every language parser individually: the caller sees a string URL, the
server sees a route it exposes, and no syntactic edge connects them. This is the same shape as the
gRPC/Protobuf contract seam, but for HTTP — and it is **not** app-specific: it recurs on every tier.

Crucially, the seam is **language-agnostic on both ends**:

- **Callers** issue requests in many idioms/languages — JS/TS (`fetch`, `axios`, `$.ajax`,
  `$.get/$.post`), C# (`HttpClient`), Python (`requests`), Go (`net/http`), Java (`RestTemplate`).
- **Endpoints** are exposed in many frameworks — ASP.NET MVC actions and Web API `[Http*]`/`[Route]`
  today; Express/Flask/Spring later.

A pairwise stitch ("JS→C#") would force N² one-off joins (JS→C#, TS→C#, C#→C#, …) and still miss the
multi-hop chains the system needs — e.g. React → BFF → platform service (a `PROXIES_TO` chain of
three HTTP hops in three language pairs). We want **one** mechanism.

Today the graph has the endpoint half only, and partially: C# emits `APIEndpoint(route, http_method)`
for attribute-routed methods; plain MVC actions resolve by the `/Controller/Action` convention (view
tier, `kendo.py`). No parser records outbound HTTP *calls*, so the call→endpoint edge cannot exist.

## Decisions

Model the seam as **a generic HTTP call joined to an endpoint by route + verb**, in the three
standard layers, so adding a language is additive and the join is written once.

1. **Parse (per language, additive).** Each language parser recognizes *its* HTTP-client idioms and
   records a generic fact on the call: the request's `http_url` (string/template) and `http_method`
   (GET/POST/… , inferred from a verb-named call or an options object; default GET). The
   *expensive* argument walk is **gated behind a cheap callee-name check**, so only actual HTTP-client
   calls pay for it — no per-call regression across large repos. P1 implements the languages in play:
   JS/TS first, then C# `HttpClient` (for the BFF / service-to-service hops).

2. **Endpoint (shared contract, mostly present).** Endpoints are `APIEndpoint(route, http_method)`
   nodes (a doc/schema label) linked to their handler `Method`. C# already emits these for
   attribute-routed methods; plain MVC actions are reachable via the `/Controller/Action` convention.
   Other endpoint-exposing languages emit the *same* `APIEndpoint` later — no new concept.

   **A declaration and a call are ONE node, not two (dedup).** An attribute-routed declaration keys its
   `APIEndpoint` by the *same* route-keyed id a caller uses (`extern://http/<VERB>:<route>` via
   `core.http_seam`), not by a per-method `{method_id}.endpoint` id. So a declared endpoint and any
   caller of that route+verb **merge on ingest** — one node, `CALLS_API` in from the caller and
   `EXPOSES_ENDPOINT` in from the handler — instead of a declared node sitting beside a separate caller
   placeholder. Routes are normalized identically on both sides (lowercased, leading-slash-rooted), so
   `[HttpGet("api/x")]` and `GetAsync("/api/x")` land on the same id. A **convention-routed** MVC action
   (no attribute route) emits *no* declared node — its URL is `/Controller/Action`, which the stitch
   forms from the call side; emitting one here would just duplicate the caller's node. A **parameterized**
   route keeps two nodes on purpose — the concrete request `/api/x/5` and the template endpoint
   `/api/x/{id}` are different things, linked by the stitch, not a duplicate.

3. **Stitch (one language-agnostic hook).** After ingestion, a single reconcile hook joins each HTTP
   call to its endpoint(s) by **route + verb** and emits `CALLS_API` (or `PROXIES_TO` when the caller
   is a gateway/BFF). It neither knows nor cares about the caller/endpoint languages.
   - **Order-independent + cross-repo (ADR 0004).** An unresolved call points at a **route-keyed
     placeholder `APIEndpoint`** (`resolved:false`); when the real endpoint with a matching route is
     ingested — any repo, any order — it is promoted/merged in place. `forget` demotes it back.
   - **1-to-many is a feature.** A call whose route matches endpoints in *both* the monolith and a
     platform service emits an edge to each, with confidence — the "does this capability already
     exist as a service?" signal.
   - **Route matching, staged.** Exact/convention routes first (MVC `/Controller/Action`,
     string-equal — the monolith case); a route-template matcher (`/api/x/{id}` ↔ `/api/x/5`) for
     attribute-routed Web API is a follow-up. Route normalization lives **once**, in the hook.

Reuse the doc/existing vocabulary: `APIEndpoint`, `CALLS_API`, `PROXIES_TO`. No new node label —
the caller-side unresolved endpoint is a placeholder `APIEndpoint`, not a bespoke `HttpCall` node.

## Consequences

- **One join for the whole system**: monolith Kendo/JS→C#, React/TS→BFF, BFF→platform, service→service
  — all via the same hook. Adding a caller/endpoint language is a per-parser fact, hook unchanged.
- Directly satisfies the cross-seam/`PROXIES_TO` requirements without N² pairwise stitches.
- **Cost**: the shared route-matcher (template ↔ concrete URL) is real work, isolated to the hook;
  the per-language caller parse must be perf-gated (name-check before arg-walk).
- **One node per endpoint** (no declared-vs-called duplication). `resolved` is a pure function of the
  graph — recomputed by the hook from whether *any* handler exposes the endpoint — so it's independent
  of ingest order and of which side wrote the node.
- **Shared-node lifecycle.** Because the endpoint id is route-keyed, it sits *outside* any single
  project's id-prefix, so a project's own resync can't reclaim it (the same property caller placeholders
  already had). The hook GCs an endpoint once it has neither a caller nor a handler — the residue of a
  route or call removed from the code, or of a forgotten repo. It does so right after clearing its own
  `http-stitch` edges, so only a declared handler counts; a stitched one would be re-added each run.
- Non-goal (for now): request/response *body* shape inference; only the routing edge is synthesized.
