"""HTTP call<->endpoint stitch (ADR 0006).

Callers (any language) materialize each outbound request as a route-keyed placeholder
`APIEndpoint{resolved:false, provenance:"http-derived"}` they `CALLS_API`-> . This pass resolves
each placeholder to its handler `Method`(s) across the shared multi-repo graph -- by route + verb,
never by symbol -- and links them with `EXPOSES_ENDPOINT`, so the golden path reads:

    caller -CALLS_API-> APIEndpoint <-EXPOSES_ENDPOINT- handler Method

Language-agnostic: it neither knows nor cares that the caller is JS and the handler C#.
Idempotent + order-independent: it owns its layer (the `http-stitch` EXPOSES_ENDPOINT edges + the
`resolved` flag), rebuilt from the current graph each run, so JS-first vs C#-first converge and a
re-ingest self-corrects. 1-to-many is deliberate -- a route matching handlers in two repos
(monolith + platform service) links both, the capability-existence signal (doc S6.1).
"""

from __future__ import annotations

import logging

from auryn.core.graph_client import GraphWriter, semantic_label
from auryn.core.graph_model import GraphEdge
from auryn.ingestors.http.schema import NODE_SCHEMAS

logger = logging.getLogger("auryn.http")


def run(writer: GraphWriter, repo, target_sha: str) -> dict | None:
    """GraphEnrichment entrypoint. `repo`/`target_sha` are unused -- the stitch reads only the
    finished graph. Returns None when there are no HTTP-derived placeholders to resolve."""
    return stitch(writer)


def stitch(writer: GraphWriter) -> dict | None:
    # An attribute-routed endpoint declaration and a caller that reaches the same route+verb now key
    # to the SAME route-keyed APIEndpoint (core.http_seam), so they arrive already merged -- one node,
    # no duplicate. This pass only has to (a) reach the endpoints that DON'T converge by id: MVC
    # `/Controller/Action` conventions and `/api/x/5` -> `api/x/{id}` templates; (b) set every
    # endpoint's `resolved` from whether a handler exposes it; (c) GC endpoints left with neither a
    # caller nor a handler (e.g. a route deleted from the code).
    if not writer.ro_query("MATCH (e:APIEndpoint) RETURN e.id LIMIT 1").result_set:
        return None  # nothing to stitch -- also guards the empty-graph "operation on empty key"

    writer.setup_schema(NODE_SCHEMAS)
    _clear_prior_layer(writer)

    placeholders = writer.ro_query(
        "MATCH (e:APIEndpoint {provenance:'http-derived'}) RETURN e.id, e.route, e.http_method"
    ).result_set
    real_endpoints = _real_endpoint_index(writer)  # (template segs, verb, handler id, endpoint id) -- once
    edges: list[GraphEdge] = []
    resolved_ids: list[str] = []
    ambiguous: list[str] = []
    unresolved: list[str] = []
    for endpoint_id, route, http_method in placeholders:
        handlers = _resolve_handlers(writer, endpoint_id, route, http_method, real_endpoints)
        if not handlers:
            unresolved.append(route)
            continue
        resolved_ids.append(endpoint_id)
        if len({method_id for method_id, _, _ in handlers}) > 1:
            ambiguous.append(route)  # matches >1 repo -> the "does this already exist as a service?" signal
        for method_id, matched_endpoint_id, handler_label in handlers:
            if matched_endpoint_id == endpoint_id:
                # Exact-route declaration: the handler already EXPOSES this very node (that's why it
                # merged). Re-adding an `http-stitch` edge would overwrite the declared edge's
                # provenance, and the next run's _clear would then delete it -- so leave it alone.
                continue
            # A handler is a Method (C#/Java controllers) or a Function (Python FastAPI/Flask routes
            # synthesized by pyframework); use its real label so the MERGE matches the right node.
            edges.append(GraphEdge("EXPOSES_ENDPOINT", handler_label, {"id": method_id},
                                   "APIEndpoint", {"id": endpoint_id}, {"provenance": "http-stitch"}))

    writer.merge_edges(edges)
    _recompute_resolved(writer)
    orphans_removed = _gc_orphan_endpoints(writer)

    report = {
        "placeholders": len(placeholders),
        "resolved": len(resolved_ids),
        "unresolved_routes": unresolved,
        "ambiguous_routes": ambiguous,
        "exposes_edges": len(edges),
        "orphans_removed": orphans_removed,
    }
    logger.info("http-endpoints: %d placeholders, %d resolved, %d exposes edges (%d ambiguous, %d orphans GC'd)",
                len(placeholders), len(resolved_ids), len(edges), len(ambiguous), orphans_removed)
    return report


def _clear_prior_layer(writer: GraphWriter) -> None:
    """Recompute-from-scratch: drop this hook's EXPOSES_ENDPOINT edges. A declared (language-parser)
    edge has no `http-stitch` provenance and survives; `resolved` is rebuilt afterward by
    _recompute_resolved, so it isn't reset here."""
    writer.query("MATCH ()-[r:EXPOSES_ENDPOINT]->() WHERE r.provenance = 'http-stitch' DELETE r")


def _recompute_resolved(writer: GraphWriter) -> None:
    """`resolved` = does any handler expose this endpoint? Recomputed over every route-keyed endpoint
    (both caller placeholders and merged declarations) so the flag is a pure function of the current
    graph -- independent of ingest order and of which side (call or declaration) wrote the node."""
    writer.query(
        "MATCH (e:APIEndpoint) WHERE e.id STARTS WITH 'extern://http/' "
        "OPTIONAL MATCH ()-[x:EXPOSES_ENDPOINT]->(e) "
        "WITH e, count(x) AS handlers SET e.resolved = handlers > 0"
    )


def _gc_orphan_endpoints(writer: GraphWriter) -> int:
    """Delete route-keyed endpoints left with neither a caller (CALLS_API) nor a handler
    (EXPOSES_ENDPOINT, from any node kind). These are the residue of a removed route or a removed
    call: the `extern://http/` node lives outside any single project's id-prefix, so a project resync
    can't reclaim it -- this does. Scoped to `extern://http/` so it never touches a project-scoped
    endpoint another ingestor owns (e.g. pyframework's `Function`-exposed routes, which that ingestor's
    own resync manages). An endpoint that is merely uncalled (declared, no caller) keeps its EXPOSES
    and is NOT an orphan."""
    orphans = writer.ro_query(
        "MATCH (e:APIEndpoint) WHERE e.id STARTS WITH 'extern://http/' "
        "AND NOT ()-[:EXPOSES_ENDPOINT]->(e) AND NOT ()-[:CALLS_API]->(e) "
        "RETURN e.id"
    ).result_set
    if orphans:
        writer.query("MATCH (e:APIEndpoint) WHERE e.id IN $ids DETACH DELETE e",
                     {"ids": [r[0] for r in orphans]})
    return len(orphans)


def _segments(route: str) -> list[str]:
    return [s for s in route.lower().split("/") if s]


def _controller_action(route: str) -> tuple[str, str] | None:
    """`/api/Widgets/List` -> ("widgetscontroller", "list") by the MVC `/Controller/Action`
    convention (last two path segments). None if the route has fewer than two segments."""
    segs = _segments(route)
    if len(segs) < 2:
        return None
    return f"{segs[-2]}controller", segs[-1]


def _verb_compatible(call_verb: str, endpoint_verb: str) -> bool:
    """A call verb matches an endpoint verb if either side is ANY (unknown / any-verb action)."""
    return call_verb == "ANY" or endpoint_verb == "ANY" or call_verb == endpoint_verb


def _route_matches(call_segs: list[str], template_segs: list[str]) -> bool:
    """Concrete call route vs an endpoint route template, segment-wise. A `{param}` segment
    matches any single segment (`/api/widgets/5` matches `api/widgets/{id}`)."""
    if len(call_segs) != len(template_segs):
        return False
    return all(t.startswith("{") and t.endswith("}") or c == t for c, t in zip(call_segs, template_segs))


def _real_endpoint_index(writer: GraphWriter) -> list[tuple[list[str], str, str, str, str]]:
    """(template segments, verb, handler id, endpoint id, handler label) for every DECLARED endpoint,
    fetched once so route-template matching is an in-memory pass, not a query per placeholder. A
    declared endpoint is identified by a non-`http-stitch` EXPOSES_ENDPOINT edge (the language
    parser's own), not by node provenance -- a merged exact-route node carries the caller's
    `http-derived` provenance yet is still a real declaration by virtue of that edge. The handler is
    a Method (C#/Java controllers) or a Function (Python FastAPI/Flask routes from pyframework), so
    match both and carry the real label back for the EXPOSES edge. The endpoint id lets the caller
    loop skip the exact-merge case (where the placeholder IS the declared node)."""
    rows = writer.ro_query(
        "MATCH (m)-[r:EXPOSES_ENDPOINT]->(real:APIEndpoint) "
        "WHERE (m:Method OR m:Function) AND real.route IS NOT NULL "
        "AND coalesce(r.provenance, '') <> 'http-stitch' "
        f"RETURN real.route, coalesce(real.http_method, 'ANY'), m.id, real.id, {semantic_label('m')}"
    ).result_set
    return [(_segments(route), str(verb).upper(), mid, rid, str(lbl)) for route, verb, mid, rid, lbl in rows]


def _resolve_handlers(
    writer: GraphWriter,
    placeholder_id: str,
    route: str,
    http_method,
    real_endpoints: list[tuple[list[str], str, str, str, str]],
) -> list[tuple[str, str | None, str]]:
    """(handler id, matched declared-endpoint id | None, handler label) for a route, across all repos
    (1-to-many). Two strategies: the MVC `/Controller/Action` convention (the caller's own node is the
    endpoint, so matched-id is None), and a verb-aware route-template match against declared endpoints
    (`/api/x/5` ↔ `api/x/{id}`; matched-id is that declaration's id -- equal to `placeholder_id` only
    on an exact-route merge). The label ("Method" or "Function") lets the EXPOSES edge MERGE against
    the handler's real node kind."""
    handlers: list[tuple[str, str | None, str]] = []
    seen: set[tuple[str, str | None]] = set()

    def add(method_id: str, endpoint_id: str | None, label: str) -> None:
        if (method_id, endpoint_id) not in seen:
            seen.add((method_id, endpoint_id))
            handlers.append((method_id, endpoint_id, label))

    ca = _controller_action(route)
    if ca is not None:
        controller_class, action = ca
        rows = writer.ro_query(
            "MATCH (c:Class)-[:HAS_METHOD]->(m:Method) "
            "WHERE toLower(c.name) = $cc AND (toLower(m.name) = $a OR toLower(m.action_name) = $a) "
            "RETURN DISTINCT m.id",
            {"cc": controller_class, "a": action},
        ).result_set
        for r in rows:
            add(r[0], None, "Method")  # MVC actions are always Methods on a controller Class

    call_segs = _segments(route)
    call_verb = str(http_method or "ANY").upper()
    for template_segs, endpoint_verb, method_id, real_id, handler_label in real_endpoints:
        if _verb_compatible(call_verb, endpoint_verb) and _route_matches(call_segs, template_segs):
            add(method_id, real_id, handler_label)

    return handlers
