"""The gRPC/Protobuf contract stitch (auryn-grpc): a `.proto` service contract is parsed from the
ingest source, emitted as RpcService/RpcMethod nodes, and a server handler + a client call are linked
to it across languages -- caller -CALLS_RPC-> RpcMethod <-IMPLEMENTS_RPC- handler. Needs FalkorDB."""

from __future__ import annotations

import git

from auryn.core import ingest
from auryn.core.graph_client import GraphWriter
from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.ingestors.grpc.proto_parser import parse_proto
from auryn.ingestors.grpc.stitch import stitch


def _commit(repo_dir, files, message):
    repo = git.Repo.init(str(repo_dir), initial_branch="main") if not (repo_dir / ".git").exists() \
        else git.Repo(str(repo_dir))
    with repo.config_writer() as cw:
        cw.set_value("user", "email", "t@e.com")
        cw.set_value("user", "name", "T")
    repo.index.add(files)
    return repo.index.commit(message).hexsha


_PROTO = """
syntax = "proto3";
package myapp.v1;

// The user service.
service UserService {
  rpc GetUser(GetUserRequest) returns (User);
  rpc ListUsers(ListUsersRequest) returns (stream User);  // server-streaming
}

message User { string id = 1; }
"""


class _FakeSource:
    """Duck-typed ingest source exposing just list_files/read_blob over an in-memory {path: text}."""
    def __init__(self, files: dict[str, str]):
        self._files = files

    def list_files(self, _sha, suffix, _excluded):
        return [(p, p) for p in self._files if p.endswith(suffix)]

    def read_blob(self, blob_sha):
        return self._files[blob_sha].encode()


def test_parse_proto_extracts_services_and_rpcs():
    pf = parse_proto(_PROTO)
    assert pf.package == "myapp.v1"
    assert [s.name for s in pf.services] == ["UserService"]
    methods = {m.name: (m.request_type, m.response_type) for m in pf.services[0].methods}
    assert methods == {"GetUser": ("GetUserRequest", "User"), "ListUsers": ("ListUsersRequest", "User")}


def test_parse_proto_ignores_commented_out_service():
    pf = parse_proto("package p;\n// service Ghost { rpc X(A) returns (B); }\nservice Real { rpc Y(A) returns (B); }\n")
    assert [s.name for s in pf.services] == ["Real"]


def test_parse_proto_ignores_special_chars_in_option_strings():
    # a `}` inside an option string must not close the service early; a URL's `//` must not eat the rest.
    pf = parse_proto('package p;\nservice S {\n  option (note) = "}";\n  rpc A(X) returns (Y);\n}\n')
    assert [s.name for s in pf.services] == ["S"] and [m.name for m in pf.services[0].methods] == ["A"]
    pf2 = parse_proto('service T { option x = "http://e.test"; rpc B(X) returns (Y); }')
    assert pf2.services and [m.name for m in pf2.services[0].methods] == ["B"]


def _seed_server(w: GraphWriter):
    """A Python-style server: UserServiceImpl EXTENDS the generated UserServiceServicer, method GetUser."""
    w.merge_node(GraphNode("Class", {"id": "python://app/UserServiceServicer"}, {"name": "UserServiceServicer"}))
    w.merge_node(GraphNode("Class", {"id": "python://app/UserServiceImpl"}, {"name": "UserServiceImpl"}))
    w.merge_edge(GraphEdge("EXTENDS", "Class", {"id": "python://app/UserServiceImpl"},
                           "Class", {"id": "python://app/UserServiceServicer"}))
    w.merge_node(GraphNode("Method", {"id": "python://app/UserServiceImpl.GetUser"}, {"name": "GetUser"}))
    w.merge_edge(GraphEdge("HAS_METHOD", "Class", {"id": "python://app/UserServiceImpl"},
                           "Method", {"id": "python://app/UserServiceImpl.GetUser"}))


def _seed_client(w: GraphWriter):
    """A client: the generated UserServiceStub has method GetUser, called by a function do_call."""
    w.merge_node(GraphNode("Class", {"id": "go://app/UserServiceStub"}, {"name": "UserServiceStub"}))
    w.merge_node(GraphNode("Method", {"id": "go://app/UserServiceStub.GetUser"}, {"name": "GetUser"}))
    w.merge_edge(GraphEdge("HAS_METHOD", "Class", {"id": "go://app/UserServiceStub"},
                           "Method", {"id": "go://app/UserServiceStub.GetUser"}))
    w.merge_node(GraphNode("Function", {"id": "go://app/do_call"}, {"name": "do_call"}))
    w.merge_edge(GraphEdge("CALLS", "Function", {"id": "go://app/do_call"},
                           "Method", {"id": "go://app/UserServiceStub.GetUser"}))


def test_stitch_builds_contract_and_links_server_and_client(graph):
    w = GraphWriter(graph)
    _seed_server(w)
    _seed_client(w)

    report = stitch(w, _FakeSource({"proto/user.proto": _PROTO}), "sha")
    assert report is not None
    assert report["services"] == 1 and report["rpc_methods"] == 2

    def q(cypher):
        return [r[0] for r in graph.query(cypher).result_set]

    # contract nodes exist, keyed by proto FQN.
    assert q("MATCH (s:RpcService {fqn:'myapp.v1.UserService'})-[:HAS_RPC]->(m:RpcMethod) RETURN m.name") \
        and sorted(q("MATCH (:RpcService {fqn:'myapp.v1.UserService'})-[:HAS_RPC]->(m) RETURN m.name")) == ["GetUser", "ListUsers"]
    # server handler -> the GetUser RpcMethod
    assert q("MATCH (:Method {name:'GetUser'})-[:IMPLEMENTS_RPC]->(m:RpcMethod {name:'GetUser'}) "
             "WHERE m.id = 'grpc://myapp.v1.UserService/GetUser' RETURN 1") == [1]
    # client caller -> the GetUser RpcMethod
    assert q("MATCH (:Function {name:'do_call'})-[:CALLS_RPC]->(m:RpcMethod {name:'GetUser'}) RETURN 1") == [1]


def test_stitch_is_idempotent(graph):
    w = GraphWriter(graph)
    _seed_server(w)
    src = _FakeSource({"proto/user.proto": _PROTO})
    stitch(w, src, "sha")
    first = graph.query("MATCH ()-[r:IMPLEMENTS_RPC]->() RETURN count(r)").result_set[0][0]
    stitch(w, src, "sha")  # a second run rebuilds its own layer -- no duplicates
    second = graph.query("MATCH ()-[r:IMPLEMENTS_RPC]->() RETURN count(r)").result_set[0][0]
    assert first == second == 1


def test_stitch_no_protos_returns_none(graph):
    w = GraphWriter(graph)
    _seed_server(w)
    assert stitch(w, _FakeSource({"README.md": "# no protos"}), "sha") is None


def test_no_proto_source_keeps_contract_and_stitches_cross_repo(graph):
    """A later ingest of a repo with NO .proto must NOT wipe a contract an earlier proto repo defined
    (run_post_hooks runs after every ingest); instead it stitches THIS repo's handlers against the
    EXISTING graph contract -- the cross-repo case. PR #24 review findings L58/L59."""
    w = GraphWriter(graph)
    stitch(w, _FakeSource({"user.proto": _PROTO}), "sha")   # contract-only repo first
    assert graph.query("MATCH (s:RpcService) RETURN count(s)").result_set[0][0] == 1
    _seed_server(w)                                          # a server-only repo (no protos of its own)
    report2 = stitch(w, _FakeSource({"README.md": "# no protos"}), "sha2")
    assert report2 is not None                              # stitched against the existing contract
    assert graph.query("MATCH (s:RpcService) RETURN count(s)").result_set[0][0] == 1  # contract survives
    assert graph.query(
        "MATCH (:Method {name:'GetUser'})-[:IMPLEMENTS_RPC]->() RETURN count(*)").result_set[0][0] >= 1


def test_ruby_snake_case_method_matches_proto_camelcase(graph):
    """Ruby snake_cases rpc names (`SayHello` -> `say_hello`); matching must normalize underscores so the
    handler still links. PR #24 review finding."""
    w = GraphWriter(graph)
    w.merge_node(GraphNode("Class", {"id": "ruby://app/GreeterServer"},
                           {"name": "GreeterServer", "superclass": "Greeter::Service"}))
    w.merge_node(GraphNode("Method", {"id": "ruby://app/GreeterServer.say_hello"}, {"name": "say_hello"}))
    w.merge_edge(GraphEdge("HAS_METHOD", "Class", {"id": "ruby://app/GreeterServer"},
                           "Method", {"id": "ruby://app/GreeterServer.say_hello"}))
    stitch(w, _FakeSource({"g.proto": "package h;\nservice Greeter { rpc SayHello(R) returns (U); }\n"}), "sha")
    assert graph.query(
        "MATCH (:Method {name:'say_hello'})-[:IMPLEMENTS_RPC]->(m:RpcMethod {name:'SayHello'}) RETURN 1"
    ).result_set == [[1]]


def test_same_short_name_across_packages_stay_distinct(graph):
    """`v1.UserService` and `v2.UserService` are separate contracts; a handler of short-named UserService
    links to the matching rpc in EACH package (1-to-many), never a merged fictitious service. PR #24."""
    w = GraphWriter(graph)
    _seed_server(w)  # UserServiceImpl EXTENDS UserServiceServicer, method GetUser
    stitch(w, _FakeSource({
        "v1.proto": "package v1;\nservice UserService { rpc GetUser(R) returns (U); }\n",
        "v2.proto": "package v2;\nservice UserService { rpc GetUser(R) returns (U); rpc Del(R) returns (U); }\n",
    }), "sha")
    fqns = sorted(r[0] for r in graph.query("MATCH (s:RpcService) RETURN s.fqn").result_set)
    assert fqns == ["v1.UserService", "v2.UserService"]
    linked = sorted(r[0] for r in graph.query(
        "MATCH (:Method {name:'GetUser'})-[:IMPLEMENTS_RPC]->(m) RETURN m.id").result_set)
    assert linked == ["grpc://v1.UserService/GetUser", "grpc://v2.UserService/GetUser"]


def test_client_stub_not_matched_as_server(graph):
    """A generated `UserServiceStub` exposing every rpc must NOT get IMPLEMENTS_RPC via the method-set
    heuristic -- it is a CLIENT, not a handler. PR #24 review finding."""
    w = GraphWriter(graph)
    w.merge_node(GraphNode("Class", {"id": "py://app/UserServiceStub"}, {"name": "UserServiceStub"}))
    for m in ("GetUser", "ListUsers"):
        w.merge_node(GraphNode("Method", {"id": f"py://app/UserServiceStub.{m}"}, {"name": m}))
        w.merge_edge(GraphEdge("HAS_METHOD", "Class", {"id": "py://app/UserServiceStub"},
                               "Method", {"id": f"py://app/UserServiceStub.{m}"}))
    stitch(w, _FakeSource({"u.proto": _PROTO}), "sha")  # UserService{GetUser, ListUsers}
    assert graph.query(
        "MATCH (:Class {name:'UserServiceStub'})-[:HAS_METHOD]->(:Method)-[:IMPLEMENTS_RPC]->() RETURN count(*)"
    ).result_set[0][0] == 0


def test_ruby_service_base_single_rpc(graph):
    """Ruby `class GreeterServer < Greeter::Service` -> the superclass encodes service `Greeter`; a
    single-rpc service (no method-set heuristic) still links via the base name. PR #24 review finding."""
    w = GraphWriter(graph)
    w.merge_node(GraphNode("Class", {"id": "ruby://app/GreeterServer"},
                           {"name": "GreeterServer", "superclass": "Greeter::Service"}))
    w.merge_node(GraphNode("Method", {"id": "ruby://app/GreeterServer.say"}, {"name": "say"}))
    w.merge_edge(GraphEdge("HAS_METHOD", "Class", {"id": "ruby://app/GreeterServer"},
                           "Method", {"id": "ruby://app/GreeterServer.say"}))
    stitch(w, _FakeSource({"g.proto": "package h;\nservice Greeter { rpc say(R) returns (U); }\n"}), "sha")
    assert graph.query(
        "MATCH (:Method {name:'say'})-[:IMPLEMENTS_RPC]->(m:RpcMethod) RETURN m.id"
    ).result_set == [["grpc://h.Greeter/say"]]


def test_stitch_end_to_end_real_python_server(graph, tmp_path):
    """End-to-end over the REAL Python ingestor + the enrichment running in the ingest tail: a `.proto`
    + a server class that subclasses the generated `UserServiceServicer` must link the handler to the
    RpcMethod. Proves the real EXTENDS representation matches the server detection."""
    (tmp_path / "user.proto").write_text(_PROTO)
    (tmp_path / "server.py").write_text(
        "import user_pb2_grpc\n\n"
        "class UserServiceImpl(user_pb2_grpc.UserServiceServicer):\n"
        "    def GetUser(self, request, context):\n        return None\n\n"
        "    def ListUsers(self, request, context):\n        return None\n"
    )
    ingest.ingest_directory(GraphWriter(graph), str(tmp_path), project_name="svc")

    # the enrichment ran in the ingest tail -> handler methods link to the contract.
    linked = sorted(r[0] for r in graph.query(
        "MATCH (:Class {name:'UserServiceImpl'})-[:HAS_METHOD]->(:Method)-[:IMPLEMENTS_RPC]->(m:RpcMethod) "
        "RETURN m.name"
    ).result_set)
    assert linked == ["GetUser", "ListUsers"]


_GO_PROTO = """
syntax = "proto3";
package routeguide;
service RouteGuide {
  rpc GetFeature(Point) returns (Feature);
  rpc ListFeatures(Rectangle) returns (stream Feature);
}
"""


def test_stitch_end_to_end_go_server_method_set(graph, tmp_path):
    """A Go server whose generated base (`*_grpc.pb.go`) is EXCLUDED as generated code has no EMBEDS edge
    to detect -- so the METHOD-SET heuristic must catch it: a struct covering every rpc of the 2-rpc
    RouteGuide service implements it. (No `.pb.go` present; the handler methods are the only signal.)"""
    (tmp_path / "route_guide.proto").write_text(_GO_PROTO)
    (tmp_path / "server.go").write_text(
        "package main\n\n"
        "type routeGuideServer struct{}\n\n"
        "func (s *routeGuideServer) GetFeature(ctx Context, p *Point) (*Feature, error) { return nil, nil }\n\n"
        "func (s *routeGuideServer) ListFeatures(r *Rectangle, stream Stream) error { return nil }\n"
    )
    ingest.ingest_directory(GraphWriter(graph), str(tmp_path), project_name="rg")

    linked = sorted(r[0] for r in graph.query(
        "MATCH (:Struct {name:'routeGuideServer'})-[:HAS_METHOD]->(:Method)-[:IMPLEMENTS_RPC]->(m:RpcMethod) "
        "RETURN m.name"
    ).result_set)
    assert linked == ["GetFeature", "ListFeatures"]


def test_grpc_links_track_incremental_resync(graph, tmp_path):
    """Delta-sync: the server<->contract links are re-derived on every resync (the enrichment re-runs in
    the ingest tail after the language's incremental sync). A .proto + a server implementing both rpcs +
    an unchanged bystander .py; dropping one handler across a commit changes ONLY the server file, so the
    resync takes the INCREMENTAL path (`deep="off"` keeps the Python graph single-phase so the ast
    engine's incremental routing fires -- a `deep="inline"` graph sits at depth="deep" and the second
    ingest would silently full-recompute, defeating the test -- PR #27 review finding). After the resync
    the removed handler's IMPLEMENTS_RPC is gone, the surviving one stays, exactly one link."""
    w = GraphWriter(graph)
    repo_dir = tmp_path / "svc"
    repo_dir.mkdir()
    (repo_dir / "user.proto").write_text(_PROTO)
    (repo_dir / "helper.py").write_text("def unrelated():\n    return 1\n")   # unchanged bystander
    server = repo_dir / "server.py"
    server.write_text(
        "import user_pb2_grpc\n\n"
        "class UserServiceImpl(user_pb2_grpc.UserServiceServicer):\n"
        "    def GetUser(self, request, context):\n        return None\n\n"
        "    def ListUsers(self, request, context):\n        return None\n"
    )
    _commit(repo_dir, ["user.proto", "server.py", "helper.py"], "v1")
    ingest.ingest_directory(w, str(repo_dir), project_name="svc", deep="off", auto_install=False)

    assert sorted(r[0] for r in graph.query(
        "MATCH (:Class {name:'UserServiceImpl'})-[:HAS_METHOD]->(:Method)-[:IMPLEMENTS_RPC]->(m:RpcMethod) "
        "RETURN m.name").result_set) == ["GetUser", "ListUsers"]

    # v2: drop the ListUsers handler from server.py ONLY; helper.py untouched; commit + resync.
    server.write_text(
        "import user_pb2_grpc\n\n"
        "class UserServiceImpl(user_pb2_grpc.UserServiceServicer):\n"
        "    def GetUser(self, request, context):\n        return None\n"
    )
    _commit(repo_dir, ["server.py"], "v2 -- dropped ListUsers")
    report = ingest.ingest_directory(w, str(repo_dir), project_name="svc", deep="off", auto_install=False)

    # PROVE the resync was INCREMENTAL, not a full recompute: the unchanged helper.py was cached
    # (a full recompute re-parses every file -> files_cached == 0).
    py = next(r for r in report.languages if r.language == "python")
    assert py.result.files_cached >= 1, f"expected a partial re-parse, cached {py.result.files_cached}"

    # only the surviving handler is linked; the removed one's edge is gone.
    assert sorted(r[0] for r in graph.query(
        "MATCH (:Class {name:'UserServiceImpl'})-[:HAS_METHOD]->(:Method)-[:IMPLEMENTS_RPC]->(m:RpcMethod) "
        "RETURN m.name").result_set) == ["GetUser"]
    # exactly one IMPLEMENTS_RPC edge total -- no stale/duplicate link accumulated across the resync.
    assert graph.query("MATCH ()-[r:IMPLEMENTS_RPC]->() RETURN count(r)").result_set == [[1]]
