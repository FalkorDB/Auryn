"""gRPC contract stitch -- the cross-language RPC analog of the HTTP call<->endpoint seam.

A `.proto` file is a service CONTRACT that lives between a client and a server written in possibly
DIFFERENT languages: `service UserService { rpc GetUser(...) returns (...); }`. gRPC codegen turns it
into a client stub (`UserServiceStub`/`UserServiceClient`) the client calls, and a server base
(`UserServiceServicer`/`UserServiceImplBase`/`Unimplemented UserServiceServer`/`UserServiceBase`/Ruby
`UserService::Service`) the server subclasses. This enrichment parses the `.proto` (from the ingest
source -- no new language ingestor), emits the contract as `RpcService -HAS_RPC-> RpcMethod` nodes keyed
by the proto FQN, and links both sides by NAME -- never by symbol -- so the golden path reads:

    caller -CALLS_RPC-> RpcMethod <-IMPLEMENTS_RPC- handler Method

i.e. an agent can trace a client's RPC call across the wire to the server method that handles it, in any
language pair. Mirrors `auryn-http`/`auryn-orm`: idempotent + order-independent (owns its whole `grpc://`
contract layer + its IMPLEMENTS_RPC/CALLS_RPC edges, rebuilt from the current source each run).

Matching is PER (service, method) NAME across packages: `v1.UserService` and `v2.UserService` stay
distinct (a handler of a short-named `UserService` links to the matching method in EACH package -- the
1-to-many "which version does this implement" signal), never merged into one fictitious service.

v1 limitation: ingesting a repo whose `.proto` files were REMOVED leaves its contract until a repo WITH
protos rebuilds the layer -- a no-proto ingest no longer wipes another repo's contract (it returns early)."""

from __future__ import annotations

import logging

from auryn.core.graph_client import GraphWriter, semantic_label
from auryn.core.graph_model import EdgeSchema, GraphEdge, GraphNode, NodeSchema
from auryn.ingestors.grpc.proto_parser import parse_proto

logger = logging.getLogger("auryn.grpc")

_EXCLUDED_DIR_NAMES = frozenset(
    {".git", "node_modules", "vendor", "dist", "build", "target", ".idea", ".vscode"})

_SCHEMA = [
    NodeSchema(label="RpcService", indexed_properties=("name", "fqn")),
    NodeSchema(label="RpcMethod", indexed_properties=("name",)),
]
_EDGES = [EdgeSchema(rel_type="HAS_RPC"), EdgeSchema(rel_type="IMPLEMENTS_RPC"), EdgeSchema(rel_type="CALLS_RPC")]

_STUB_SUFFIXES = ("BlockingStub", "FutureStub", "Stub", "Client")  # client-side generated stub names
_TYPE_LABELS = "(c:Class OR c:Struct OR c:Interface OR c:Record)"


def run(writer: GraphWriter, source, target_sha: str) -> dict | None:
    """GraphEnrichment entrypoint. Reads `.proto` from the ingest `source`, builds the contract, and
    stitches the code graph to it. Returns None when there are no `.proto` services to tie."""
    return stitch(writer, source, target_sha)


def stitch(writer: GraphWriter, source, target_sha: str) -> dict | None:
    protos = _read_protos(source, target_sha)
    writer.setup_schema(_SCHEMA)
    _clear_links(writer)  # always re-stitch the link edges from scratch (idempotent)
    if any(pf.services for _, pf in protos):
        _clear_contract(writer)  # this source owns the contract -> rebuild it from the current .proto set
        nodes, edges, rpc_by_key, rpcsets, present = _build_contract(protos)
        writer.merge_nodes(nodes)
        writer.merge_edges(edges)
    else:
        # No .proto in THIS source. Do NOT wipe a contract another repo defined (run_post_hooks runs after
        # every ingest) -- instead stitch THIS repo's handlers/callers against the EXISTING graph contract,
        # so a server-only or client-only repo still links cross-repo (Copilot PR #24 findings L58/L59).
        rpc_by_key, rpcsets, present = _read_contract_from_graph(writer)
        if not present:
            return None  # no contract anywhere in the graph

    link_edges = _stitch_servers(writer, rpc_by_key, rpcsets, present) + _stitch_clients(writer, rpc_by_key, present)
    if link_edges:
        writer.merge_edges(link_edges)

    n_impl = sum(1 for e in link_edges if e.rel_type == "IMPLEMENTS_RPC")
    report = {"services": len(present), "rpc_methods": sum(len(v) for v in rpc_by_key.values()),
              "implements_edges": n_impl, "calls_edges": len(link_edges) - n_impl}
    logger.info("gRPC stitch: %s", report)
    return report


def _read_protos(source, target_sha: str):
    """[(path, ProtoFile)] parsed from every `.proto` the ingest source lists. Duck-typed on
    list_files/read_blob (GitRepoCache or LocalDirectorySource), tolerant of read/parse failure."""
    out = []
    try:
        entries = list(source.list_files(target_sha, ".proto", set(_EXCLUDED_DIR_NAMES)))
    except Exception:
        return out
    for path, blob_sha in entries:
        try:
            text = source.read_blob(blob_sha).decode("utf-8", "replace")
        except Exception:
            continue
        out.append((path, parse_proto(text)))
    return out


def _build_contract(protos):
    """Emit RpcService/RpcMethod nodes (keyed by proto FQN `grpc://<pkg>.<Service>[/<Method>]`) + the
    HAS_RPC spine. Returns, for the stitch:
      rpc_by_key: (service_lower, method_lower) -> [method_id]  (a list: same short name in >1 package)
      rpcsets:    [(service_lower, fqn, frozenset(method_lower))] for services with >=2 rpcs (method-set heuristic)
      present:    {service_lower} for the base-name guard."""
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    rpc_by_key: dict[tuple[str, str], list[str]] = {}
    rpcsets: list[tuple[str, str, frozenset]] = []
    present: set[str] = set()
    for _path, pf in protos:
        for svc in pf.services:
            fqn = f"{pf.package}.{svc.name}" if pf.package else svc.name
            sid = f"grpc://{fqn}"
            svc_lower = svc.name.lower()
            present.add(svc_lower)
            nodes.append(GraphNode("RpcService", {"id": sid},
                                   {"name": svc.name, "fqn": fqn, "package": pf.package, "provenance": "grpc-contract"}))
            mset: set[str] = set()
            for meth in svc.methods:
                mid = f"{sid}/{meth.name}"
                nodes.append(GraphNode("RpcMethod", {"id": mid},
                                       {"name": meth.name, "service": svc.name, "fqn": fqn,
                                        "request_type": meth.request_type, "response_type": meth.response_type,
                                        "provenance": "grpc-contract"}))
                edges.append(GraphEdge("HAS_RPC", "RpcService", {"id": sid}, "RpcMethod", {"id": mid}))
                rpc_by_key.setdefault((svc_lower, _norm_method(meth.name)), []).append(mid)
                mset.add(_norm_method(meth.name))
            if len(mset) >= 2:
                rpcsets.append((svc_lower, fqn, frozenset(mset)))
    return nodes, edges, rpc_by_key, rpcsets, present


def _service_candidates(base_name: str) -> list[str]:
    """Service name(s) a generated server-base class name could encode. Handles the qualified forms as
    written -- `user_pb2_grpc.UserServiceServicer`, `UserServiceGrpc.UserServiceImplBase`, Ruby
    `Greeter::Service` (service is the segment BEFORE `Service`), Go `Unimplemented<S>Server`, C#
    `<S>Base`/`<S>Server`."""
    parts = base_name.replace("::", ".").strip(".").split(".")
    simple = parts[-1] if parts else ""
    out: list[str] = []
    if simple == "Service" and len(parts) >= 2:  # Ruby/C++ `<Service>::Service`
        out.append(parts[-2])
    if simple.startswith("Unimplemented") and simple.endswith("Server"):
        out.append(simple[len("Unimplemented"):-len("Server")])
    for suf in ("Servicer", "ImplBase", "Base", "Server", "Service"):
        if simple.endswith(suf) and len(simple) > len(suf):
            out.append(simple[:-len(suf)])
    return [c for c in out if c]


def _is_generated_grpc_type(name: str) -> bool:
    """True for an UNAMBIGUOUSLY generated gRPC type name -- a client stub (`*Stub`/`*Client`) or a
    generated server base that is NEVER a user class: `*Servicer` (Python), `*ImplBase` (Java),
    `Unimplemented*` (the Go base struct a server embeds). Excludes these from the method-set server
    heuristic so a retained generated stub/base isn't mistaken for a handler (Copilot PR #24 finding).
    The AMBIGUOUS `*Server`/`*Base`/`*Service` are deliberately NOT excluded -- they are the usual USER
    impl names (Go `routeGuideServer`, Ruby `GreeterService`); the narrow cost is that a RETAINED generated
    Go interface/C# base could still method-set-match (only with generated code retained, not the default)."""
    return bool(_service_from_stub(name)) or name.startswith("Unimplemented") \
        or any(name.endswith(s) for s in ("Servicer", "ImplBase"))


def _stitch_servers(writer, rpc_by_key, rpcsets, present) -> list[GraphEdge]:
    """Link each server impl's handler methods to the RpcMethod(s) they implement. Detection:
    1. BASE NAME (precise): the impl subclasses/embeds the generated base (inheritance EDGES -- Java
       EXTENDS, Go EMBEDS -- AND the base-name PROPERTIES stamped for EXTERNAL bases: Python `bases`,
       Ruby `superclass`). A type may implement SEVERAL services (multiple matching bases) -- all kept.
    2. METHOD SET (fallback): a non-generated Class/Struct whose methods cover every rpc of a >=2-rpc
       service implements it -- catches servers whose generated base is EXCLUDED as generated code."""
    # all candidate types + name + methods, fetched once (the enrichment only runs when .proto exist).
    type_name: dict[str, str] = {}
    type_methods: dict[str, list[tuple[str, str, str]]] = {}  # id -> [(method id, name, label)]
    for cid, cname, mid, mname, mlabel in writer.ro_query(
        f"MATCH (c)-[:HAS_METHOD]->(m) WHERE {_TYPE_LABELS} "
        f"RETURN c.id, c.name, m.id, m.name, {semantic_label('m')}"
    ).result_set:
        type_name[str(cid)] = str(cname) if cname is not None else ""
        type_methods.setdefault(str(cid), []).append((str(mid), str(mname), str(mlabel)))

    impl: dict[str, set[str]] = {}  # type id -> {service_lower it implements}

    def consider_base(tid: str, base_name) -> None:
        if not base_name:
            return
        for cand in _service_candidates(str(base_name)):
            if cand.lower() in present:
                impl.setdefault(tid, set()).add(cand.lower())

    for cid, bname in writer.ro_query(
        f"MATCH (c)-[:EXTENDS|IMPLEMENTS|EMBEDS]->(b) WHERE {_TYPE_LABELS} AND b.name IS NOT NULL RETURN c.id, b.name"
    ).result_set:
        consider_base(str(cid), bname)
    for cid, bases in writer.ro_query("MATCH (c) WHERE c.bases IS NOT NULL RETURN c.id, c.bases").result_set:
        for b in (bases or []):
            consider_base(str(cid), b)
    for cid, sup in writer.ro_query("MATCH (c) WHERE c.superclass IS NOT NULL RETURN c.id, c.superclass").result_set:
        consider_base(str(cid), sup)

    # method-set fallback (skip generated stub/base types, and types a base signal already matched).
    for cid, methods in type_methods.items():
        if _is_generated_grpc_type(type_name.get(cid, "")):
            continue
        mset = {_norm_method(n) for _, n, _ in methods}
        for svc_lower, _fqn, rpcset in rpcsets:
            if not (impl.get(cid) and svc_lower in impl[cid]) and rpcset <= mset:
                impl.setdefault(cid, set()).add(svc_lower)

    edges: list[GraphEdge] = []
    for cid, svcs in impl.items():
        for mid, mname, mlabel in type_methods.get(cid, ()):
            for svc_lower in svcs:
                for rpc in rpc_by_key.get((svc_lower, _norm_method(mname)), ()):
                    edges.append(GraphEdge("IMPLEMENTS_RPC", mlabel, {"id": mid},
                                           "RpcMethod", {"id": rpc}, {"provenance": "grpc-stitch"}))
    return edges


def _stitch_clients(writer, rpc_by_key, present) -> list[GraphEdge]:
    """Best-effort: a client calls `stub.Method()` where stub's type is the generated `<Service>Stub`/
    `Client`. Find methods of stub-named types matching an RpcMethod, then their callers -> CALLS_RPC.
    Links only when the generated stub class is in the graph (often excluded as generated code)."""
    stub_methods: dict[str, list[str]] = {}  # stub method id -> [rpc method ids]
    for oname, mid, mname in writer.ro_query(
        "MATCH (o)-[:HAS_METHOD]->(m) WHERE (o:Class OR o:Struct OR o:Interface) AND o.name IS NOT NULL "
        "RETURN o.name, m.id, m.name"
    ).result_set:
        svc = _service_from_stub(str(oname))
        if svc and svc.lower() in present:
            rpcs = rpc_by_key.get((svc.lower(), _norm_method(str(mname))))
            if rpcs:
                stub_methods[str(mid)] = rpcs
    if not stub_methods:
        return []
    edges: list[GraphEdge] = []
    seen: set[tuple[str, str]] = set()
    for caller, clabel, mid in writer.ro_query(
        f"MATCH (caller)-[:CALLS]->(m) WHERE m.id IN $ids RETURN caller.id, {semantic_label('caller')}, m.id",
        {"ids": list(stub_methods)},
    ).result_set:
        for rpc in stub_methods[str(mid)]:
            key = (str(caller), rpc)
            if key not in seen:
                seen.add(key)
                edges.append(GraphEdge("CALLS_RPC", str(clabel), {"id": str(caller)},
                                       "RpcMethod", {"id": rpc}, {"provenance": "grpc-stitch"}))
    return edges


def _service_from_stub(name: str) -> str | None:
    for suf in _STUB_SUFFIXES:
        if name.endswith(suf) and len(name) > len(suf):
            return name[:-len(suf)]
    return None


def _norm_method(name: str) -> str:
    """Normalize an rpc/method name for matching: lowercase AND strip `_`, so a proto `SayHello` matches
    a Ruby handler `say_hello` (gRPC Ruby snake_cases rpc names). CamelCase names have no `_`, so this is
    a no-op for Python/Go/Java/C# handlers (they keep the proto spelling)."""
    return name.lower().replace("_", "")


def _read_contract_from_graph(writer: GraphWriter):
    """Rebuild (rpc_by_key, rpcsets, present) from RpcService/RpcMethod nodes ALREADY in the graph -- used
    when the current source has no `.proto` but a contract from another repo is present, so a server-/
    client-only repo still stitches against it (cross-repo). Mirrors `_build_contract`'s return shape."""
    rpc_by_key: dict[tuple[str, str], list[str]] = {}
    present: set[str] = set()
    by_fqn: dict[tuple[str, str], set[str]] = {}
    for sname, fqn, mname, mid in writer.ro_query(
        "MATCH (s:RpcService)-[:HAS_RPC]->(m:RpcMethod) RETURN s.name, s.fqn, m.name, m.id"
    ).result_set:
        svc_lower = str(sname).lower()
        present.add(svc_lower)
        rpc_by_key.setdefault((svc_lower, _norm_method(str(mname))), []).append(str(mid))
        by_fqn.setdefault((svc_lower, str(fqn)), set()).add(_norm_method(str(mname)))
    rpcsets = [(svc, fqn, frozenset(ms)) for (svc, fqn), ms in by_fqn.items() if len(ms) >= 2]
    return rpc_by_key, rpcsets, present


def _clear_links(writer: GraphWriter) -> None:
    """Drop this hook's link edges (re-stitched every run). The contract nodes are cleared separately."""
    writer.query("MATCH ()-[r:IMPLEMENTS_RPC]->() DELETE r")
    writer.query("MATCH ()-[r:CALLS_RPC]->() DELETE r")


def _clear_contract(writer: GraphWriter) -> None:
    """Drop the RpcService/RpcMethod contract nodes (grpc-only labels this hook owns) -- only when the
    current source re-defines the contract from its own `.proto` files."""
    writer.query("MATCH (n:RpcMethod) DETACH DELETE n")
    writer.query("MATCH (n:RpcService) DETACH DELETE n")
