"""The Go route enrichment (goframework, `go-routes`) and Go's HTTP callers in the cross-language seam
(ADR 0006): a Go service's routes become route-keyed APIEndpoints exposed by their handlers, and its
outbound net/http requests become CALLS_API, so the http-endpoints stitch joins Go to TS callers and to
Python handlers in one graph. Needs FalkorDB (the conftest fixture); tree-sitter only (deep="off")."""

from __future__ import annotations

import git

from auryn.core import graph_queries, ingest
from auryn.core.graph_client import GraphWriter
from auryn.ingestors.goframework.synthesize import run


def _commit(repo_dir, files, message):
    repo = git.Repo.init(str(repo_dir), initial_branch="main") if not (repo_dir / ".git").exists() \
        else git.Repo(str(repo_dir))
    with repo.config_writer() as cw:
        cw.set_value("user", "email", "t@e.com")
        cw.set_value("user", "name", "T")
    repo.index.add(files)
    return repo.index.commit(message).hexsha


def _write(root, files: dict[str, str]):
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


_ORDERS = {
    "go.mod": "module example.com/orders\n\ngo 1.22\n",
    "main.go": (
        "package main\n\n"
        'import (\n\t"net/http"\n\n\t"example.com/orders/handlers"\n)\n\n'
        "func main() {\n"
        "\tmux := http.NewServeMux()\n"
        '\tmux.HandleFunc("POST /api/orders", handlers.CreateOrder)\n'
        '\thttp.ListenAndServe(":8080", mux)\n'
        "}\n"
    ),
    "handlers/orders.go": (
        "package handlers\n\n"
        'import (\n\t"net/http"\n\n\t"example.com/orders/catalog"\n)\n\n'
        "func CreateOrder(w http.ResponseWriter, r *http.Request) {\n\tcatalog.Reserve()\n}\n"
    ),
    "catalog/client.go": (
        "package catalog\n\n"
        'import "net/http"\n\n'
        "func Reserve() error {\n"
        '\t_, err := http.Post("http://catalog:8000/api/catalog/reserve", "application/json", nil)\n'
        "\treturn err\n"
        "}\n"
    ),
}
_WEB = {"api.ts": 'export async function submitOrder() {\n  await fetch("/api/orders", { method: "POST" });\n}\n'}
_CATALOG = {
    "app.py": (
        "from fastapi import FastAPI\n\napp = FastAPI()\n\n\n"
        '@app.post("/api/catalog/reserve")\ndef reserve():\n    return {"ok": True}\n'
    ),
}


def test_go_service_joins_ts_callers_and_python_handlers(graph, tmp_path):
    """The headline: a TS `fetch` reaches a Go net/http handler, and that handler's Go client reaches a
    Python FastAPI route -- three repos, three languages, one graph. The Go service is ingested FIRST, so
    its outbound call starts unresolved and resolves once the Python repo lands (order independence)."""
    w = GraphWriter(graph)
    for name, files in (("orders", _ORDERS), ("web", _WEB), ("catalog", _CATALOG)):
        _write(tmp_path / name, files)

    ingest.ingest_directory(w, str(tmp_path / "orders"), deep="off", auto_install=False)
    assert graph.query(
        "MATCH (:Function {name:'Reserve'})-[:CALLS_API]->(e:APIEndpoint) RETURN e.id, e.resolved"
    ).result_set == [["extern://http/POST:/api/catalog/reserve", False]]

    ingest.ingest_directory(w, str(tmp_path / "web"), deep="off", auto_install=False)
    ingest.ingest_directory(w, str(tmp_path / "catalog"), deep="off", auto_install=False)

    # TS -> Go: the caller and the Go declaration share ONE route-keyed node.
    assert graph.query(
        "MATCH (:Function {name:'submitOrder'})-[:CALLS_API]->(e:APIEndpoint)"
        "<-[x:EXPOSES_ENDPOINT]-(h:Function {name:'CreateOrder'}) "
        "RETURN e.id, e.resolved, x.provenance, x.framework"
    ).result_set == [["extern://http/POST:/api/orders", True, "goframework", "net/http"]]
    # Go -> Python: the Go client's placeholder is resolved to the FastAPI handler by the stitch.
    assert graph.query(
        "MATCH (:Function {name:'Reserve'})-[:CALLS_API]->(e:APIEndpoint)"
        "<-[:EXPOSES_ENDPOINT]-(h:Function {name:'reserve'}) RETURN e.route, e.resolved"
    ).result_set == [["/api/catalog/reserve", True]]
    # Both seams collapse into traversable INVOKES edges.
    assert sorted(graph.query(
        "MATCH (c)-[i:INVOKES]->(h) RETURN c.name, h.name, i.via"
    ).result_set) == [["Reserve", "reserve", "http"], ["submitOrder", "CreateOrder", "http"]]

    # ...so one trace from the TS function crosses both hops into the Python handler.
    entry = graph.query("MATCH (f:Function {name:'submitOrder'}) RETURN f.id").result_set[0][0]
    names = {f["name"] for f in graph_queries.trace_entrypoint(graph, entry)["functions"]}
    assert {"CreateOrder", "Reserve", "reserve"} <= names


def test_handler_reference_shapes_resolve_to_their_nodes(graph, tmp_path):
    """Each handler shape lands on the right node: a cross-package func, a method value on the
    receiver, a `&T{}` Handler (its ServeHTTP), a method on a field of unknown type (the project's one
    method by that name), an inline func literal (the registering function), and a gin group route."""
    _write(tmp_path, {
        "go.mod": "module example.com/shop\n\ngo 1.22\n",
        "server.go": (
            "package main\n\n"
            'import (\n\t"net/http"\n\n\t"github.com/gin-gonic/gin"\n\t"example.com/shop/api"\n)\n\n'
            "type Server struct{ orders *api.Orders }\n\n"
            "type Stats struct{}\n\n"
            "func (Stats) ServeHTTP(w http.ResponseWriter, r *http.Request) {}\n\n"
            "func (s *Server) health(w http.ResponseWriter, r *http.Request) {}\n\n"
            "func (s *Server) routes(mux *http.ServeMux) {\n"
            '\tmux.HandleFunc("GET /api/items", api.ListItems)\n'
            '\tmux.HandleFunc("GET /health", s.health)\n'
            '\tmux.Handle("/api/stats", &Stats{})\n'
            '\tmux.HandleFunc("DELETE /api/orders/{id}", s.orders.Cancel)\n'
            '\tmux.HandleFunc("/ping", func(w http.ResponseWriter, r *http.Request) {})\n'
            '\tmux.HandleFunc("/api/missing", NoSuchHandler)\n'
            "}\n\n"
            "func ginRoutes() {\n"
            "\tr := gin.Default()\n"
            '\tv1 := r.Group("/v1")\n'
            '\tv1.PUT("/items/:id", api.UpdateItem)\n'
            "}\n"
        ),
        "api/items.go": (
            "package api\n\n"
            'import "net/http"\n\n'
            "type Orders struct{}\n\n"
            "func ListItems(w http.ResponseWriter, r *http.Request) {}\n\n"
            "func UpdateItem(c any) {}\n\n"
            "func (o *Orders) Cancel(w http.ResponseWriter, r *http.Request) {}\n"
        ),
    })
    ingest.ingest_directory(GraphWriter(graph), str(tmp_path), deep="off", auto_install=False)

    rows = graph.query(
        "MATCH (h)-[x:EXPOSES_ENDPOINT {provenance:'goframework'}]->(e:APIEndpoint) "
        "RETURN e.http_method + ' ' + e.route, h.name, x.framework ORDER BY e.route, e.http_method"
    ).result_set
    assert rows == [
        ["GET /api/items", "ListItems", "net/http"],
        ["DELETE /api/orders/{id}", "Cancel", "net/http"],
        ["ANY /api/stats", "ServeHTTP", "net/http"],
        ["GET /health", "health", "net/http"],
        ["ANY /ping", "routes", "net/http"],
        ["PUT /v1/items/{id}", "UpdateItem", "gin"],
    ]
    # the route whose handler names nothing in the graph gets no endpoint (no guess)
    assert graph.query("MATCH (e:APIEndpoint {route:'/api/missing'}) RETURN count(e)").result_set == [[0]]
    # an endpoint is a trace entry point: entering it starts from its handler
    trace = graph_queries.trace_entrypoint(graph, "extern://http/GET:/api/items")
    assert [f["name"] for f in trace["functions"]] == ["ListItems"]


def test_run_is_idempotent_and_leaves_other_layers_alone(graph, tmp_path):
    _write(tmp_path, {"main.go": (
        'package main\n\nimport "net/http"\n\n'
        'func main() { http.HandleFunc("/api/x", X) }\n\nfunc X(w http.ResponseWriter, r *http.Request) {}\n'
    )})
    w = GraphWriter(graph)
    ingest.ingest_directory(w, str(tmp_path), deep="off", auto_install=False)
    # a declared edge from another layer onto the same route-keyed node must survive the rerun
    graph.query("CREATE (:Method {id:'cs://m', name:'M'})")
    graph.query("MATCH (m:Method {id:'cs://m'}), (e:APIEndpoint {id:'extern://http/ANY:/api/x'}) "
                "CREATE (m)-[:EXPOSES_ENDPOINT]->(e)")
    before = graph.query("MATCH ()-[r:EXPOSES_ENDPOINT]->() RETURN count(r)").result_set
    run(w, None, "")
    run(w, None, "")
    assert graph.query("MATCH ()-[r:EXPOSES_ENDPOINT]->() RETURN count(r)").result_set == before == [[2]]


def test_go_routes_track_incremental_resync(graph, tmp_path):
    """Delta-sync: the route moves in main.go while handlers/ stays put (so the resync is INCREMENTAL),
    then the handler's signature changes in handlers/ while main.go stays put (its node id changes, the
    registering function is not re-emitted). Each time the endpoint follows: the old route's node is
    gone, and the handler is found by name where it is now."""
    w = GraphWriter(graph)
    repo = tmp_path / "svc"
    _write(repo, {
        "go.mod": "module example.com/svc\n\ngo 1.22\n",
        "main.go": (
            'package main\n\nimport (\n\t"net/http"\n\n\t"example.com/svc/handlers"\n)\n\n'
            'func main() { http.HandleFunc("GET /api/orders/{id}", handlers.Get) }\n'
        ),
        "handlers/h.go": (
            'package handlers\n\nimport "net/http"\n\nfunc Get(w http.ResponseWriter, r *http.Request) {}\n'
        ),
        "other.go": "package main\n\nfunc other() int { return 1 }\n",
    })
    _commit(repo, ["go.mod", "main.go", "handlers/h.go", "other.go"], "v1")
    ingest.ingest_directory(w, str(repo), project_name="svc", deep="off", auto_install=False)

    def exposed():
        return graph.query(
            "MATCH (h:Function)-[:EXPOSES_ENDPOINT]->(e:APIEndpoint) RETURN e.id, h.id ORDER BY e.id"
        ).result_set

    assert [e for e, _ in exposed()] == ["extern://http/GET:/api/orders/{id}"]

    (repo / "main.go").write_text(
        'package main\n\nimport (\n\t"net/http"\n\n\t"example.com/svc/handlers"\n)\n\n'
        'func main() { http.HandleFunc("GET /api/invoices/{id}", handlers.Get) }\n'
    )
    _commit(repo, ["main.go"], "v2 -- route moved")
    report = ingest.ingest_directory(w, str(repo), project_name="svc", deep="off", auto_install=False)
    go = next(r for r in report.languages if r.language == "go")
    assert go.result.files_cached >= 1, f"expected a partial re-parse, cached {go.result.files_cached}"
    assert [e for e, _ in exposed()] == ["extern://http/GET:/api/invoices/{id}"]
    assert graph.query("MATCH (e:APIEndpoint) RETURN count(e)").result_set == [[1]]  # old one reclaimed

    (repo / "handlers/h.go").write_text(
        'package handlers\n\nimport "net/http"\n\n'
        "func Get(w http.ResponseWriter, req *http.Request, extra int) {}\n"
    )
    _commit(repo, ["handlers/h.go"], "v3 -- handler signature changed")
    ingest.ingest_directory(w, str(repo), project_name="svc", deep="off", auto_install=False)
    rows = exposed()
    assert len(rows) == 1 and rows[0][1].endswith(".func.Get(http.ResponseWriter,*http.Request,int)")

    (repo / "main.go").write_text("package main\n\nfunc main() {}\n")
    _commit(repo, ["main.go"], "v4 -- route removed")
    ingest.ingest_directory(w, str(repo), project_name="svc", deep="off", auto_install=False)
    assert exposed() == []
    assert graph.query("MATCH (e:APIEndpoint) RETURN count(e)").result_set == [[0]]
    assert graph.query(
        "MATCH (f:Function {name:'main'}) RETURN f.http_routes IS NULL"
    ).result_set == [[True]]
