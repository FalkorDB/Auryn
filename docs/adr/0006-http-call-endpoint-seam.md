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

## Go (callers and endpoints)

Go takes part on both ends, through the same route-keyed node.

- **Callers.** The Go parser records `http.Get/Head/Post/PostForm(url, …)`, `http.DefaultClient.Get(…)`,
  the same verbs on a parameter or local known to be an `*http.Client`, and
  `http.NewRequest(verb, url, …)` / `http.NewRequestWithContext(ctx, verb, url, …)`. The last two cover
  a `client.Do(req)` flow: `Do` carries neither verb nor URL, so the request is keyed where it is built.
  The URL is a string literal, a `fmt.Sprintf` format or a `+` chain, each dynamic part becoming
  `{param}` (as a TS template string or a C# interpolation does). Resolution emits `CALLS_API` to the
  placeholder with the same props as the TS and C# callers.
- **Path-only key.** A Go service names its peer by a deployment host (`http://catalog:8080/api/x`),
  which no handler declares, so Go keys a request by its path (`core.http_seam.strip_origin`, then
  `normalize_route`). TS and C# still key an absolute URL as written, so a TS `fetch("http://host/x")`
  does not resolve yet.
- **Endpoints.** A Go route is registered away from its handler (`mux.HandleFunc("/x", CreateOrder)` in
  `main`, `CreateOrder` in another file). The parser records each registration on the registering
  `Function`/`Method` as an `http_routes` entry: verb, normalized route, a handler reference
  (`inline`, `func:Name`, `func:<import path>.Name`, `method:[<import path>.][Type.]Name`) and the
  framework. The `go-routes` enrichment (`packages/go`, `auryn.ingestors.goframework`) resolves the
  handler by name against the current graph on every run, so a handler whose node id changed in an
  incremental sync is still found. It writes the endpoint route-keyed (like C#'s declared routes), so it
  merges with any caller's placeholder, and owns only its `provenance:"goframework"` `EXPOSES_ENDPOINT`
  edges (they also carry `framework`).
- **Frameworks.** net/http `HandleFunc`/`Handle` on any mux, with Go 1.22 patterns (`"POST /x/{id}"`,
  a host prefix, `{rest...}`, `{$}`); gin, echo and chi verb methods plus `Handle`/`Add`/`Method`;
  gorilla/mux `HandleFunc(…).Methods(…)`. Param syntax (`:id`, `*path`, `{id:[0-9]+}`, `{rest...}`) is
  rendered `{name}` for the stitch's template matcher. A `&T{}` handler resolves to `T.ServeHTTP`;
  `http.HandlerFunc`, `http.StripPrefix`, gin `WrapF`/`WrapH` and echo `WrapHandler` are seen through.
- **Group prefixes (v1).** Joined when set up in the same function: gin/echo `Group`, gorilla
  `PathPrefix(…).Subrouter()`, chi `Route`/`Group` closures and `With`. Not followed: a router passed in
  as a parameter or kept in a field (its routes keep their written path), chi `Mount`, net/http
  `StripPrefix` mounts. A handler built by a constructor call (`mux.Handle("/x", NewHandler(db))`) or
  passed through a middleware call other than those above gets no endpoint rather than a guess.

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
  route or call removed from the code.
- Non-goal (for now): request/response *body* shape inference; only the routing edge is synthesized.
