"""The Java web-framework enrichment (javaframework): Spring MVC controller annotations become
APIEndpoint + EXPOSES_ENDPOINT, so a Java handler is a first-class HTTP entry point the cross-language
http-endpoints stitch can resolve against any-language callers. Needs FalkorDB (the conftest fixture)."""

from __future__ import annotations

import git

from auryn.core import ingest
from auryn.core.graph_client import GraphWriter
from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.ingestors.javaframework.synthesize import _base_path, _join, _mappings, _route_arg, _verbs, run


def _commit(repo_dir, files, message):
    repo = git.Repo.init(str(repo_dir), initial_branch="main") if not (repo_dir / ".git").exists() \
        else git.Repo(str(repo_dir))
    with repo.config_writer() as cw:
        cw.set_value("user", "email", "t@e.com")
        cw.set_value("user", "name", "T")
    repo.index.add(files)
    return repo.index.commit(message).hexsha


def test_route_arg_three_valued():
    # _route_arg takes the INNER annotation args (what _balanced_args returns) and is 3-valued:
    # a path string / "" (no path -> class base) / None (unresolvable constant -> skip).
    assert _route_arg('"/users/{id}"') == "/users/{id}"
    assert _route_arg('value = "/x", method = RequestMethod.POST') == "/x"
    assert _route_arg('path = "/y"') == "/y"
    assert _route_arg('{"/a", "/b"}') == "/a"
    assert _route_arg('produces = "application/json"') == ""      # kwarg-only -> base, not a path
    assert _route_arg("") == ""                                   # no args -> base
    assert _route_arg("Routes.USERS") is None                     # positional constant -> unresolvable
    assert _route_arg("path = Routes.USERS") is None              # value/path= non-string -> unresolvable


def test_verbs_from_annotation():
    assert _verbs("Get", "") == ["GET"]
    assert _verbs("Request", "(method = RequestMethod.GET)") == ["GET"]
    assert _verbs("Request", "(method = {RequestMethod.GET, RequestMethod.POST})") == ["GET", "POST"]
    assert _verbs("Request", '("/x")') == ["ANY"]


def test_mappings_and_join():
    ms = _mappings('@GetMapping("/users/{id}")\n  public String get(int id){return "";}')
    assert ms == [(["GET"], "/users/{id}")]
    assert _join("/api", "/users/{id}") == "/api/users/{id}"
    assert _join("api", "users") == "/api/users"
    assert _join("/api", "") == "/api"


def test_mappings_regex_path_variable_balanced_parens():
    # a `)` inside a quoted Spring regex path var must not truncate the path (PR #26 review finding)
    ms = _mappings('@GetMapping("/items/{kind:(foo|bar)}")\n  public String g(){return "";}')
    assert ms == [(["GET"], "/items/{kind:(foo|bar)}")]


def test_mappings_skips_unresolvable_constant_path():
    # a constant path (no string literal, no method=) is skipped, not emitted at the controller base
    assert _mappings("@GetMapping(Routes.USERS)\n  public String g(){return \"\";}") == []
    # but a verb-only @RequestMapping(method=...) with no path is kept (route = base)
    assert _mappings("@RequestMapping(method = RequestMethod.GET)\n  public String g(){return \"\";}") == [(["GET"], "")]
    # and a bare @GetMapping with no args is kept (route = base)
    assert _mappings('@GetMapping\n  public String g(){return "";}') == [(["GET"], "")]


def test_base_path_reads_only_class_header():
    # the base must come from the class-level @RequestMapping, NOT a method's @GetMapping in the body
    base, verbs = _base_path('public class C {\n  @GetMapping("/users")\n  public String g(){return "";}\n}')
    assert base == "" and verbs == ["ANY"]
    base2, verbs2 = _base_path('@RequestMapping(value = "/api", method = RequestMethod.GET)\npublic class C {\n  @GetMapping("/x")\n}')
    assert base2 == "/api" and verbs2 == ["GET"]
    # a `{` inside the class-level route (`/tenants/{tenantId}`) must not truncate the header (PR #26 review)
    base3, _ = _base_path('@RequestMapping("/tenants/{tenantId}")\npublic class C {\n  @GetMapping("/orders")\n}')
    assert base3 == "/tenants/{tenantId}"
    # a `class`/`record` keyword inside an annotation arg or a route string is NOT the header boundary
    base4, _ = _base_path('@Validated(Group.class)\n@RequestMapping("/api/record")\npublic class C {\n  @GetMapping("/x")\n}')
    assert base4 == "/api/record"


def _seed_controller(w: GraphWriter):
    w.merge_node(GraphNode("Class", {"id": "java://app/Ctrl"}, {
        "name": "Ctrl",
        "source": '@RestController\n@RequestMapping("/api")\npublic class Ctrl {\n'
                  '  @GetMapping("/users/{id}")\n  public String get(int id){return "";}\n'
                  '  @PostMapping("/users")\n  public void create(){}\n}'}))
    for mid, name, src in [
        ("java://app/Ctrl.get", "get", '@GetMapping("/users/{id}")\n  public String get(int id){return "";}'),
        ("java://app/Ctrl.create", "create", '@PostMapping("/users")\n  public void create(){}'),
    ]:
        w.merge_node(GraphNode("Method", {"id": mid}, {"name": name, "source": src}))
        w.merge_edge(GraphEdge("HAS_METHOD", "Class", {"id": "java://app/Ctrl"}, "Method", {"id": mid}))


def test_synthesize_spring_controller(graph):
    w = GraphWriter(graph)
    _seed_controller(w)
    report = run(w, None, "sha")
    assert report is not None and report["endpoints"] == 2

    def q(c):
        return sorted(f"{r[0]} {r[1]}" for r in graph.query(c).result_set)

    # class base `/api` is prefixed onto each method path, verb from the mapping annotation.
    assert q("MATCH (:Method {name:'get'})-[:EXPOSES_ENDPOINT]->(e:APIEndpoint) RETURN e.http_method, e.route") \
        == ["GET /api/users/{id}"]
    assert q("MATCH (:Method {name:'create'})-[:EXPOSES_ENDPOINT]->(e:APIEndpoint) RETURN e.http_method, e.route") \
        == ["POST /api/users"]


def test_synthesize_base_less_controller_route_stands_alone(graph):
    """A controller with NO class-level @RequestMapping must not take its first method's route as the base
    (would double it to /users/users). PR #26 review finding."""
    w = GraphWriter(graph)
    w.merge_node(GraphNode("Class", {"id": "java://app/C"}, {
        "name": "C", "source": 'public class C {\n  @GetMapping("/users")\n  public String g(){return "";}\n}'}))
    w.merge_node(GraphNode("Method", {"id": "java://app/C.g"},
                           {"name": "g", "source": '@GetMapping("/users")\n  public String g(){return "";}'}))
    w.merge_edge(GraphEdge("HAS_METHOD", "Class", {"id": "java://app/C"}, "Method", {"id": "java://app/C.g"}))
    run(w, None, "sha")
    assert [r[0] for r in graph.query(
        "MATCH (:Method {name:'g'})-[:EXPOSES_ENDPOINT]->(e) RETURN e.route").result_set] == ["/users"]


def test_synthesize_is_idempotent(graph):
    w = GraphWriter(graph)
    _seed_controller(w)
    run(w, None, "sha")
    first = graph.query("MATCH (e:APIEndpoint {provenance:'javaframework'}) RETURN count(e)").result_set[0][0]
    run(w, None, "sha")
    second = graph.query("MATCH (e:APIEndpoint {provenance:'javaframework'}) RETURN count(e)").result_set[0][0]
    assert first == second == 2


def test_end_to_end_real_java_controller(graph, tmp_path):
    """End-to-end over the real Java ingestor + the enrichment in the ingest tail: a Spring controller
    yields route-keyed APIEndpoints with the class base prefixed."""
    (tmp_path / "Ctrl.java").write_text(
        'package app;\n@RestController\n@RequestMapping("/api/v1")\npublic class OrderController {\n'
        '    @GetMapping("/orders/{id}")\n    public String get(int id) { return ""; }\n\n'
        '    @PostMapping("/orders")\n    public void create() { }\n}\n'
    )
    ingest.ingest_directory(GraphWriter(graph), str(tmp_path), project_name="orders")

    routes = sorted(f"{r[0]} {r[1]}" for r in graph.query(
        "MATCH (:Method)-[:EXPOSES_ENDPOINT]->(e:APIEndpoint {provenance:'javaframework'}) "
        "RETURN e.http_method, e.route"
    ).result_set)
    assert routes == ["GET /api/v1/orders/{id}", "POST /api/v1/orders"]


def test_cross_language_ts_caller_reaches_java_handler(graph, tmp_path):
    """The headline: a TS client `fetch('/api/orders/5')` resolves to the Java Spring handler of
    `@GetMapping('/orders/{id}')` -- the java-routes + http-endpoints enrichments together tie a
    cross-language call to its server handler (caller -CALLS_API-> endpoint <-EXPOSES_ENDPOINT- handler)."""
    (tmp_path / "OrderController.java").write_text(
        'package app;\n@RestController\n@RequestMapping("/api")\npublic class OrderController {\n'
        '    @GetMapping("/orders/{id}")\n    public String get(int id) { return ""; }\n}\n'
    )
    (tmp_path / "client.ts").write_text(
        "export async function loadOrder(id: number) {\n"
        "  const res = await fetch(`/api/orders/5`);\n  return res.json();\n}\n"
    )
    ingest.ingest_directory(GraphWriter(graph), str(tmp_path), project_name="shop")

    # the TS caller's route-keyed endpoint is exposed by the Java handler (route-template match in the stitch).
    linked = graph.query(
        "MATCH (caller)-[:CALLS_API]->(e:APIEndpoint)<-[:EXPOSES_ENDPOINT]-(h:Method) "
        "WHERE h.name = 'get' RETURN count(*)"
    ).result_set[0][0]
    assert linked >= 1


def test_java_routes_track_incremental_resync(graph, tmp_path):
    """Delta-sync: the Spring route endpoints are re-derived on every resync (the enrichment re-runs in
    the ingest tail after the Java ingestor's incremental sync). The controller changes while a bystander
    .java file does not, so the resync takes the INCREMENTAL path (the unchanged file is cached, not
    re-parsed). Java is single-phase so `deep` doesn't gate its incremental routing, but we pass
    `deep="off"` for consistency with the other seams' resync tests (PR #27 review finding). After the
    resync the old route's endpoint is gone and the new one is the single declared node -- no duplicate."""
    w = GraphWriter(graph)
    repo_dir = tmp_path / "svc"
    repo_dir.mkdir()
    ctrl = repo_dir / "OrderController.java"
    (repo_dir / "Other.java").write_text(            # unchanged bystander
        "package app;\npublic class Other { public int x() { return 1; } }\n")

    def write(route):
        ctrl.write_text(
            'package app;\n@RestController\n@RequestMapping("/api")\npublic class OrderController {\n'
            f'    @GetMapping("{route}")\n    public String get(int id) {{ return ""; }}\n}}\n'
        )

    write("/orders/{id}")
    _commit(repo_dir, ["OrderController.java", "Other.java"], "v1")
    ingest.ingest_directory(w, str(repo_dir), project_name="orders", deep="off", auto_install=False)
    assert graph.query(
        "MATCH (e:APIEndpoint {provenance:'javaframework'}) RETURN e.route"
    ).result_set == [["/api/orders/{id}"]]

    write("/invoices/{id}")
    _commit(repo_dir, ["OrderController.java"], "v2 -- route moved")
    report = ingest.ingest_directory(w, str(repo_dir), project_name="orders", deep="off", auto_install=False)

    # PROVE the resync was INCREMENTAL, not a full recompute: the unchanged Other.java was cached
    # (a full recompute re-parses every file -> files_cached == 0).
    jr = next(r for r in report.languages if r.language == "java")
    assert jr.result.files_cached >= 1, f"expected a partial re-parse, cached {jr.result.files_cached}"

    # exactly one javaframework endpoint, on the NEW route -- the old one didn't linger as a duplicate.
    assert graph.query(
        "MATCH (e:APIEndpoint {provenance:'javaframework'}) RETURN e.route"
    ).result_set == [["/api/invoices/{id}"]]
    # ...and it is still exposed by the handler method (the EXPOSES_ENDPOINT edge tracked the change).
    assert graph.query(
        "MATCH (:Method {name:'get'})-[:EXPOSES_ENDPOINT]->(e:APIEndpoint {route:'/api/invoices/{id}'}) "
        "RETURN count(*)"
    ).result_set == [[1]]
