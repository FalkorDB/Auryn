"""Java web-framework enrichment: the Spring (and Spring-Boot/JAX-RS-style) analogue of pyframework --
a post-pass that turns Spring MVC route annotations into `APIEndpoint` + `EXPOSES_ENDPOINT`, so a Java
controller method becomes a first-class HTTP entry point that the cross-language `http-endpoints` stitch
then resolves against ANY-language callers (`caller -CALLS_API-> APIEndpoint <-EXPOSES_ENDPOINT- handler`).

Java controllers were previously the one big web stack with NO endpoint graph (C#, Python FastAPI/Flask,
and HTTP callers were already covered; Java had nothing). It reads each controller method's stored
`source` (the method annotation `@GetMapping("/x")` is in the Method source; the class-level base
`@RequestMapping("/api")` is in the owning Class source) -- no source re-parsing of files, like orm.

Recognizes `@GetMapping/@PostMapping/@PutMapping/@DeleteMapping/@PatchMapping` (verb from the name) and
`@RequestMapping` (verb(s) from `method = RequestMethod.X`, else ANY), with the path from the first
positional string or `value=`/`path=` (a `produces=`/`consumes=` media type is NOT mistaken for a path).
Idempotent: recomputes only its own `provenance:"javaframework"` APIEndpoint layer."""

from __future__ import annotations

import logging
import re

from auryn.core import http_seam
from auryn.core.graph_client import GraphWriter, semantic_label
from auryn.core.graph_model import EdgeSchema, GraphEdge, GraphNode, NodeSchema

logger = logging.getLogger("auryn.javaframework")

_PROVENANCE = "javaframework"
_SCHEMA = [NodeSchema(label="APIEndpoint", indexed_properties=("route",))]
_EDGES = [EdgeSchema(rel_type="EXPOSES_ENDPOINT")]

_VERB_BY_ANN = {"GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT",
                "DeleteMapping": "DELETE", "PatchMapping": "PATCH"}
_ANN_NAME = re.compile(r"@(Get|Post|Put|Delete|Patch|Request)Mapping\b")  # just the annotation name
_REQUEST_METHOD = re.compile(r"RequestMethod\.(\w+)")


def _balanced_args(text: str, start: int) -> str | None:
    """The `(...)` args immediately after `start`, matched balanced + quote-aware (so a `)` inside a
    quoted regex path var `{kind:(foo|bar)}` doesn't end the args early). None when there is no `(`."""
    i = start
    while i < len(text) and text[i] in " \t\r\n":
        i += 1
    if i >= len(text) or text[i] != "(":
        return None
    depth, j, in_str = 0, i, None
    while j < len(text):
        ch = text[j]
        if in_str:
            if ch == "\\":
                j += 2
                continue
            if ch == in_str:
                in_str = None
        elif ch in "\"'":
            in_str = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[i + 1:j]
        j += 1
    return text[i + 1:]  # unbalanced (malformed) -- take the rest


_VALUE_KW = re.compile(r"(?:\bvalue|\bpath)\s*=\s*")


def _route_arg(args: str) -> str | None:
    """The route PATH from an annotation's `(...)` args, three-valued:
      - a string -> the path (from `value=`/`path=` or the first positional string literal);
      - `""`     -> the annotation declares NO path (bare `@GetMapping`, or only kwargs like
                    `produces=`/`method=`) -> the route is the class base;
      - `None`   -> the path is an UNRESOLVABLE constant (`@GetMapping(Routes.USERS)`,
                    `@RequestMapping(path = Routes.USERS)`) -> skip (don't emit a wrong endpoint)."""
    a = (args or "").strip()
    if not a:
        return ""  # @X or @X() -> base
    m = _VALUE_KW.search(a)
    if m:  # explicit value=/path= : a string is the path, anything else is an unresolvable constant
        sm = re.match(r'\{?\s*["\']([^"\']*)["\']', a[m.end():].lstrip())
        return sm.group(1) if sm else None
    if a[0] in "\"'":  # positional string (incl. "")
        sm = re.match(r'["\']([^"\']*)["\']', a)
        return sm.group(1) if sm else None
    if a[0] == "{":  # a {"/a","/b"} positional array
        sm = re.search(r'["\']([^"\']*)["\']', a)
        return sm.group(1) if sm else None
    if re.match(r"\w+\s*=", a):  # kwargs-only (produces=/consumes=/method=), no path -> base
        return ""
    return None  # a bare positional constant (Routes.USERS) -> unresolvable


def _verbs(ann_name: str, args: str) -> list[str]:
    """HTTP verb(s) for one annotation: the @<Verb>Mapping name, or @RequestMapping's `method =
    RequestMethod.X` (possibly several), defaulting to ANY when a @RequestMapping names no method."""
    if ann_name != "Request":
        return [_VERB_BY_ANN[f"{ann_name}Mapping"]]
    verbs = [m.group(1).upper() for m in _REQUEST_METHOD.finditer(args or "")]
    return verbs or ["ANY"]


def _mappings(source: str) -> list[tuple[list[str], str]]:
    """[(verbs, path)] for every mapping annotation in a source snippet (a method). Skips an annotation
    whose path is an UNRESOLVABLE constant (`@GetMapping(Routes.USERS)` -- args present, no string literal,
    not a verb-only `@RequestMapping(method=...)`): emitting it at the controller base would be wrong."""
    out: list[tuple[list[str], str]] = []
    for m in _ANN_NAME.finditer(source or ""):
        args = _balanced_args(source, m.end())
        path = _route_arg(args or "")
        if path is None:
            continue  # an unresolvable constant path -> skip rather than materialize a wrong base endpoint
        out.append((_verbs(m.group(1), args or ""), path))
    return out


def _base_path(class_source: str) -> tuple[str, list[str]]:
    """The class-level `@RequestMapping` base path + its default verb(s), read ONLY from the class HEADER
    (before the body) so a method's own `@GetMapping("/x")` inside the body is never mistaken for the base.
    Returns ("", ["ANY"]) when the controller declares no class-level @RequestMapping path."""
    src = class_source or ""
    kw = re.search(r"\b(?:class|interface|enum|record)\s+\w", src)  # keyword + the class NAME, so `.class` in
    header = src[:kw.start()] if kw else src                       # `@Validated(Group.class)` or `record` in a route string is not a false boundary
    for m in _ANN_NAME.finditer(header):
        if m.group(1) == "Request":
            args = _balanced_args(header, m.end())
            path = _route_arg(args or "")
            if path:
                return path, _verbs("Request", args or "")
    return "", ["ANY"]


def _join(base: str, path: str) -> str:
    """Join a controller base and a method path into one normalized route."""
    combined = "/".join(p.strip("/") for p in (base, path) if p and p.strip("/"))
    return http_seam.normalize_route("/" + combined if combined else (base or path or "/"))


def run(writer: GraphWriter, repo, target_sha: str) -> dict | None:
    """GraphEnrichment entrypoint. Reads controller Class/Method source from the graph and synthesizes
    the Java endpoint layer. Returns None when there are no Spring mappings."""
    writer.setup_schema(_SCHEMA)
    _clear_prior_layer(writer)

    rows = writer.ro_query(
        f"MATCH (c:Class)-[:HAS_METHOD]->(m:Method) "
        f"WHERE m.source IS NOT NULL AND m.source CONTAINS 'Mapping' "
        f"RETURN c.id, c.source, m.id, {semantic_label('m')}, m.name, m.source"
    ).result_set

    base_by_class: dict[str, tuple[str, list[str]]] = {}
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    handler_ids: set[str] = set()
    for cid, csource, mid, mlabel, mname, msource in rows:
        cid = str(cid)
        if cid not in base_by_class:
            base_by_class[cid] = _base_path(str(csource) if csource else "")
        base, class_verbs = base_by_class[cid]
        for verbs, path in _mappings(str(msource)):
            route = _join(base, path)
            # a method with no verb of its own (`@RequestMapping("/x")`) inherits the class-level default
            # verb when the controller's `@RequestMapping` specified one (method = RequestMethod.X).
            if verbs == ["ANY"] and class_verbs != ["ANY"]:
                verbs = class_verbs
            for verb in verbs:
                endpoint_id = f"javaapi://{mid}#{verb}:{route}"
                nodes.append(GraphNode("APIEndpoint", {"id": endpoint_id},
                                       {"route": route, "http_method": verb, "framework": "spring",
                                        "name": f"{verb} {route}", "handler": str(mname), "provenance": _PROVENANCE}))
                edges.append(GraphEdge("EXPOSES_ENDPOINT", str(mlabel), {"id": str(mid)},
                                       "APIEndpoint", {"id": endpoint_id}))
                handler_ids.add(str(mid))

    if not nodes:
        return None
    writer.merge_nodes(nodes)
    writer.merge_edges(edges)
    report = {"endpoints": len(nodes), "handlers": len(handler_ids)}
    logger.info("java routes: %d endpoints across %d handlers", report["endpoints"], report["handlers"])
    return report


def _clear_prior_layer(writer: GraphWriter) -> None:
    """Recompute-from-scratch: DETACH DELETE this pass's own APIEndpoints (+ their EXPOSES_ENDPOINT),
    scoped by provenance so C#/Python endpoints in the same graph are untouched."""
    writer.query(f"MATCH (e:APIEndpoint {{provenance: '{_PROVENANCE}'}}) DETACH DELETE e")
