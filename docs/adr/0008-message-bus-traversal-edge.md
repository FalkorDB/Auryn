# ADR 0008 — Message-bus hop: a derived async edge the traversals follow

Status: **Proposed.** Date: 2026-11-22.

Builds on ADR 0006 (the HTTP seam and its derived `INVOKES` edge) and ADR 0004 (cross-repo
resolution in one shared graph).

## Context

The message-bus stitch (`auryn-events`) joins a publisher and a subscriber through the topic they
share:

```
publisher -PUBLISHES_TO-> Topic <-SUBSCRIBES_TO- subscriber
```

The Topic is a "V": both sides point *into* it. Every directed traversal — `who_calls`, `callees`,
`impact_of`, `trace_entrypoint`, a directed `find_path` — walks one way and dies at the topic. So
"what happens when an order is placed" stops at `order.placed`, even when the listener that
decrements inventory sits in the same graph. ADR 0006 hit the same shape at the HTTP boundary and
collapsed it with a derived `caller -INVOKES-> handler` edge.

The open question was whether to reuse `INVOKES` for the async hop, or add a type of its own.

## Decision

The stitch also writes a distinct derived edge, **`publisher -NOTIFIES-> subscriber`**:

- One edge per (publisher, subscriber) pair, with `topics` (every topic the two share, sorted) and
  `provenance: "message-bus"`. A callable that subscribes to its own topic gets no self-loop.
- Built from the same per-callable extraction as the raw edges, so it is a pure function of the
  current source — idempotent and order-independent. The stitch drops its `message-bus` `NOTIFIES`
  edges with the rest of its layer at the start of every run, before any early return, so a removed
  publish or subscribe takes its edge with it. The raw `PUBLISHES_TO`/`SUBSCRIBES_TO` edges and the
  `Topic` node stay, for provenance and the viewer.

Which traversals follow it:

| Traversal | Follows `NOTIFIES`? | Why |
|---|---|---|
| `who_calls` / `callees` | yes | "who sets this off" / "what does this set off" — the edge they return names the hop and its topic |
| `trace_entrypoint` | yes | the closure is "what runs as a result of this entry point" |
| `impact_of` (default edge set) | yes | downstream from a publisher reaches the subscribers a payload change can break; upstream from a subscriber reaches what triggers it |
| `find_path` (directed) | yes | it follows any type unless `edge_types` narrows it |
| `impact_of_diff` | **no** | a dependency walk — see below |

**Why not `INVOKES`.** `INVOKES` is a request the caller waits on: the caller depends on the
handler's answer. A publisher never waits on its subscribers and does not depend on them. That
difference matters to `impact_of_diff`, which walks the default edge set in reverse and reads every
edge as "this depends on the changed code". With the async hop folded into `INVOKES`, a change to a
listener would report the publisher, everything that calls it, and their services as broken. An
edge type can be left out of a walk, a property cannot (Cypher filters a variable-length pattern by
type). So the async hop gets its own type, the reachability walks include it
(`DEFAULT_CLOSURE_CALL_EDGE_TYPES`, `DEFAULT_IMPACT_EDGE_TYPES`), and `impact_of_diff` subtracts
`ASYNC_EDGE_TYPES`.

**Why the name.** `NOTIFIES` says fire-and-forget. It does not collide with the PL/SQL `Trigger`
vocabulary (`HAS_TRIGGER`), which a `TRIGGERS` edge would.

## Consequences

- One traversal now runs from a web entry point through an HTTP hop and a message-bus hop to the
  listener in another repo and language.
- Fan-out is publishers × subscribers per topic, like `INVOKES`'s 1-to-many.
- `NOTIFIES` is only as good as the topic join: a topic held in a variable or built by
  concatenation is not resolved (the stitch's existing limit), so neither is the hop.
- Not done here: `impact_of_diff` could report a changed publisher's subscribers as async consumers
  (a forward walk over `NOTIFIES` from the changed symbols and their dependents). It reports
  dependents only, as before.
