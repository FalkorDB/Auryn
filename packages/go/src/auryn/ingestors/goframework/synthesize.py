"""Go HTTP route enrichment: the net/http / gin / echo / chi / gorilla analogue of pyframework and
javaframework -- a post-pass that turns a Go service's route registrations into `APIEndpoint` +
`EXPOSES_ENDPOINT`, so its handlers are HTTP entry points that the cross-language `http-endpoints`
stitch resolves against ANY-language callers (`caller -CALLS_API-> APIEndpoint <-EXPOSES_ENDPOINT- handler`).

Go differs from those two in one way that shapes this pass: a route is registered AWAY from its handler
(`mux.HandleFunc("/x", CreateOrder)` in `main`, `CreateOrder` in another file), so the handler is a
reference to resolve, not the annotated function itself. The Go parser records each registration on the
registering Function/Method as an `http_routes` entry (`"<verb> <route> <handler> <framework>"`, see
go/raw_model.RawRoute and go/http_idioms.py); this pass resolves the handler by NAME against the current
graph each run, so a handler renamed or moved in an incremental sync is found where it is now:

- `inline`                          -> the registering callable itself (a func literal handler);
- `func:Name`                       -> the Function `Name` in the registering package;
- `func:<import path>.Name`         -> the Function `Name` in the package with that import path;
- `method:Type.Name`                -> the Method `Name` of `Type` in the registering package;
- `method:<import path>.Type.Name`  -> the Method `Name` of `Type` in that package;
- `method:Name`                     -> the one Method `Name` in the project (receiver type unknown:
                                       `s.handlers.Create`); several with that name -> the one in the
                                       registering package, else no edge (no guess).
A package match prefers the registering project, else any project in the graph (a handler package
ingested from another repo).

The endpoint is ROUTE-KEYED (core.http_seam, like C#'s declared routes), so a caller in any language
reaching the same route+verb lands on the same node. Idempotent: this pass owns only its
`provenance:"goframework"` EXPOSES_ENDPOINT edges; it drops them, writes the current ones, and then
deletes any endpoint it left with no edge at all (one the stitch didn't reach yet)."""

from __future__ import annotations

import logging

from auryn.core import http_seam
from auryn.core.graph_client import GraphWriter, semantic_label
from auryn.core.graph_model import EdgeSchema, GraphEdge, GraphNode, NodeSchema

logger = logging.getLogger("auryn.goframework")

_PROVENANCE = "goframework"
_SCHEMA = [NodeSchema(label="APIEndpoint", indexed_properties=("route",))]
_EDGES = [EdgeSchema(rel_type="EXPOSES_ENDPOINT")]  # handler Function|Method -> APIEndpoint {provenance, framework}


def run(writer: GraphWriter, repo, target_sha: str) -> dict | None:
    """GraphEnrichment entrypoint. `repo`/`target_sha` are unused -- the pass reads only the graph.
    Returns None when no Go callable registers a route."""
    writer.setup_schema(_SCHEMA)
    previous = _clear_prior_layer(writer)

    rows = writer.ro_query(
        "MATCH (f) WHERE (f:Function OR f:Method) AND f.http_routes IS NOT NULL "
        f"RETURN f.id, {semantic_label('f')}, f.http_routes"
    ).result_set

    nodes: dict[str, GraphNode] = {}
    edges: list[GraphEdge] = []
    unresolved: list[str] = []
    handlers = _Handlers(writer)
    for registrar_id, registrar_label, entries in rows:
        for entry in entries or []:
            parts = str(entry).split(" ")
            if len(parts) != 4:
                continue
            verb, route, handler, framework = parts
            targets = handlers.resolve(handler, str(registrar_id), str(registrar_label))
            if not targets:
                unresolved.append(f"{verb} {route} -> {handler}")
                continue
            endpoint_id = http_seam.endpoint_placeholder_key(verb, route)
            nodes[endpoint_id] = GraphNode("APIEndpoint", {"id": endpoint_id},
                                           {"route": route, "http_method": verb, "name": f"{verb} {route}"})
            for target_id, target_label in targets:
                edges.append(GraphEdge("EXPOSES_ENDPOINT", target_label, {"id": target_id},
                                       "APIEndpoint", {"id": endpoint_id},
                                       {"provenance": _PROVENANCE, "framework": framework}))

    if nodes:
        writer.merge_nodes(list(nodes.values()))
        writer.merge_edges(edges)
    removed = _drop_unreferenced(writer, previous - nodes.keys())
    if not nodes:
        return None
    report = {"endpoints": len(nodes), "exposes_edges": len(edges), "unresolved": unresolved,
              "endpoints_removed": removed}
    logger.info("go routes: %d endpoints, %d exposes edges (%d unresolved handlers)",
                len(nodes), len(edges), len(unresolved))
    return report


def _clear_prior_layer(writer: GraphWriter) -> set[str]:
    """Drop this pass's EXPOSES_ENDPOINT edges (scoped by provenance, so the C# parser's declared edges
    and the stitch's `http-stitch` edges stay) and return the endpoints they pointed at. The endpoint
    nodes stay: they are route-keyed and a caller may share them."""
    rows = writer.ro_query(
        "MATCH ()-[r:EXPOSES_ENDPOINT]->(e:APIEndpoint) WHERE r.provenance = $p RETURN DISTINCT e.id",
        {"p": _PROVENANCE},
    ).result_set
    writer.query("MATCH ()-[r:EXPOSES_ENDPOINT]->() WHERE r.provenance = $p DELETE r", {"p": _PROVENANCE})
    return {str(r[0]) for r in rows}


def _drop_unreferenced(writer: GraphWriter, endpoint_ids: set[str]) -> int:
    """Delete the endpoints this pass no longer exposes that nothing else points at (no caller, no other
    handler, no k8s Ingress) -- a route removed from the code. Without the http-endpoints stitch
    installed nothing else would reclaim them."""
    if not endpoint_ids:
        return 0
    rows = writer.ro_query(
        "MATCH (e:APIEndpoint) WHERE e.id IN $ids AND NOT ()-->(e) RETURN e.id", {"ids": sorted(endpoint_ids)}
    ).result_set
    if rows:
        writer.query("MATCH (e:APIEndpoint) WHERE e.id IN $ids DETACH DELETE e", {"ids": [r[0] for r in rows]})
    return len(rows)


def _project_of(package_id: str) -> str:
    """The project id a Go package id starts with: `go://shop` for `go://shop/package/api`
    (go.ids.package_id)."""
    return package_id.split("/package", 1)[0]


class _Handlers:
    """A resolver for RawRoute handler references against the graph; it caches each registering
    callable's package."""

    def __init__(self, writer: GraphWriter):
        self.writer = writer
        self._package: dict[str, str | None] = {}

    def package_of(self, callable_id: str, label: str) -> str | None:
        """The id of the Package a Function (or a Method's receiver type) belongs to."""
        if callable_id not in self._package:
            if label == "Method":
                query = "MATCH (p:Package)-[:HAS_TYPE]->()-[:HAS_METHOD]->(f:Method {id: $id}) RETURN p.id"
            else:
                query = "MATCH (p:Package)-[:HAS_FUNCTION]->(f:Function {id: $id}) RETURN p.id"
            rows = self.writer.ro_query(query, {"id": callable_id}).result_set
            self._package[callable_id] = str(rows[0][0]) if rows else None
        return self._package[callable_id]

    def resolve(self, handler: str, registrar_id: str, registrar_label: str) -> list[tuple[str, str]]:
        """[(handler id, label)] for one handler reference; [] when it names nothing in the graph."""
        if handler == "inline":
            return [(registrar_id, registrar_label)]
        package_id = self.package_of(registrar_id, registrar_label)
        if package_id is None:
            return []
        kind, _, ref = handler.partition(":")
        if kind == "func":
            import_path, _, name = ref.rpartition(".")
            return self._in_package("MATCH (p:Package)-[:HAS_FUNCTION]->(h:Function {name: $name}) ",
                                    "Function", name, None, import_path, package_id)
        if kind == "method":
            parts = ref.rsplit(".", 2)
            name = parts[-1]
            if len(parts) == 1:
                return self._unique_method(name, package_id)
            type_name = parts[-2]
            import_path = parts[0] if len(parts) == 3 else ""
            match = "MATCH (p:Package)-[:HAS_TYPE]->(t {name: $type})-[:HAS_METHOD]->(h:Method {name: $name}) "
            return self._in_package(match, "Method", name, type_name, import_path, package_id)
        return []

    def _in_package(self, match: str, label: str, name: str, type_name: str | None, import_path: str,
                    package_id: str) -> list[tuple[str, str]]:
        """The `label` named `name` (on `type_name`) in the registering package, or in the package(s)
        with `import_path` -- those of the registering project first."""
        where = "WHERE p.id = $pid " if not import_path else "WHERE p.import_path = $ip "
        rows = self.writer.ro_query(
            match + where + "AND coalesce(h.kind, '') <> 'INTERFACE_METHOD' RETURN DISTINCT h.id, p.id",
            {"name": name, "type": type_name, "pid": package_id, "ip": import_path},
        ).result_set
        project = _project_of(package_id) + "/"
        own = [str(hid) for hid, pid in rows if str(pid).startswith(project)]
        return [(hid, label) for hid in sorted(own or {str(hid) for hid, _ in rows})]

    def _unique_method(self, name: str, package_id: str) -> list[tuple[str, str]]:
        """The Method `name` when the registering project has exactly one (or exactly one in the
        registering package); [] when it is ambiguous or absent."""
        project = _project_of(package_id)
        rows = self.writer.ro_query(
            "MATCH (p:Package)-[:HAS_TYPE]->()-[:HAS_METHOD]->(h:Method {name: $name}) "
            "WHERE p.id >= $lo AND p.id < $hi AND coalesce(h.kind, '') <> 'INTERFACE_METHOD' "
            "RETURN DISTINCT h.id, p.id",
            {"name": name, "lo": project + "/", "hi": project + "/" + chr(0xFFFF)},
        ).result_set
        if len(rows) == 1:
            return [(str(rows[0][0]), "Method")]
        local = [str(hid) for hid, pid in rows if str(pid) == package_id]
        return [(local[0], "Method")] if len(local) == 1 else []
