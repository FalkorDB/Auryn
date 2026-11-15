"""Python web-framework enrichment: a post-pass that turns route *decorators* already
captured on Function/Method nodes into `APIEndpoint` entry points + `EXPOSES_ENDPOINT`
edges -- the Python analogue of the Java Spring route pass (javaframework), and the thing that makes a
request->handler->call-closure golden path traceable (and gives the agent-access layer
real Python entry points to inventory).

It reads what the language engines already produced (the `decorators` string list on each
Function/Method), so no source re-parsing: `@app.post("/widgets")` and
`@app.route("/x", methods=["POST"])` are recognised for FastAPI/Starlette/Flask. The
decorated function becomes the endpoint's handler, so trace_entrypoint(endpoint) reaches
its whole CALLS closure.

Idempotent: it recomputes only its own layer -- APIEndpoint nodes tagged
`provenance:"pyframework"` (so C#'s own APIEndpoints in the same graph are untouched) and
their EXPOSES_ENDPOINT edges.
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass

from auryn.core.git_source import GitRepoCache
from auryn.core.graph_client import GraphWriter
from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.ingestors.pyframework.schema import NODE_SCHEMAS

logger = logging.getLogger("auryn.pyframework")

_PROVENANCE = "pyframework"
_METHOD_VERBS = {"get", "post", "put", "delete", "patch", "head", "options"}


@dataclass(frozen=True)
class RouteInfo:
    http_method: str
    path: str
    framework: str


def parse_routes(decorator: str) -> list[RouteInfo]:
    """Extract route(s) from one decorator's source text. Handles verb decorators
    (`app.get("/x")`, `router.post("/x")` -- FastAPI/Starlette/Flask 2) and Flask's
    `app.route("/x", methods=["GET","POST"])` (one endpoint per method; default GET).
    Returns [] for any non-route decorator."""
    try:
        expr = ast.parse(decorator, mode="eval").body
    except SyntaxError:
        return []
    if not isinstance(expr, ast.Call) or not isinstance(expr.func, ast.Attribute):
        return []
    attr = expr.func.attr.lower()
    path = _first_str_arg(expr)
    if path is None:
        return []
    if attr in _METHOD_VERBS:
        return [RouteInfo(attr.upper(), path, "fastapi")]
    if attr == "route":
        methods = _methods_kwarg(expr) or ["GET"]
        return [RouteInfo(m, path, "flask") for m in methods]
    return []


def _first_str_arg(call: ast.Call) -> str | None:
    for arg in call.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
    return None


def _methods_kwarg(call: ast.Call) -> list[str]:
    for kw in call.keywords:
        if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
            return [e.value.upper() for e in kw.value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return []


def run(writer: GraphWriter, repo: GitRepoCache, target_sha: str) -> dict | None:
    """GraphEnrichment entrypoint. Reads Function/Method decorators from the graph and
    synthesizes the endpoint layer. Returns None when there are no Python routes."""
    writer.setup_schema(NODE_SCHEMAS)
    _clear_prior_layer(writer)

    from auryn.core.graph_client import semantic_label

    handlers = writer.ro_query(
        "MATCH (h) WHERE (h:Function OR h:Method) AND h.decorators IS NOT NULL AND size(h.decorators) > 0 "
        f"RETURN {semantic_label('h')} AS label, h.id AS id, h.name AS name, h.decorators AS decorators"
    ).result_set

    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    handler_ids: set[str] = set()
    for label, handler_id, handler_name, decorators in handlers:
        for decorator in decorators or []:
            for route in parse_routes(decorator):
                endpoint_id = f"pyapi://{handler_id}#{route.http_method}:{route.path}"
                nodes.append(
                    GraphNode(
                        "APIEndpoint",
                        {"id": endpoint_id},
                        {
                            "route": route.path,
                            "http_method": route.http_method,
                            "framework": route.framework,
                            "name": f"{route.http_method} {route.path}",
                            "handler": handler_name,
                            "provenance": _PROVENANCE,
                        },
                    )
                )
                edges.append(GraphEdge("EXPOSES_ENDPOINT", label, {"id": handler_id}, "APIEndpoint", {"id": endpoint_id}))
                handler_ids.add(handler_id)

    if not nodes:
        return None
    writer.merge_nodes(nodes)
    writer.merge_edges(edges)

    report = {"endpoints": len(nodes), "handlers": len(handler_ids)}
    logger.info("python routes: %d endpoints across %d handlers", report["endpoints"], report["handlers"])
    return report


def _clear_prior_layer(writer: GraphWriter) -> None:
    """Recompute-from-scratch: DETACH DELETE removes this pass's own APIEndpoints (tagged
    with our provenance) *and* their EXPOSES_ENDPOINT edges in one step -- scoped by
    provenance so any C# APIEndpoints in the same graph are left alone."""
    writer.query(f"MATCH (e:APIEndpoint {{provenance: '{_PROVENANCE}'}}) DETACH DELETE e")
