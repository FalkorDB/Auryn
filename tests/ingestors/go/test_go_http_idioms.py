"""The Go HTTP idioms the parser records (ADR 0006), no FalkorDB: outbound requests (`http_calls`, which
resolution turns into CALLS_API) and route registrations (`http_routes`, which the go-routes enrichment
turns into APIEndpoint + EXPOSES_ENDPOINT) for net/http, gin, echo, chi and gorilla/mux."""

from __future__ import annotations

from auryn.core import http_seam
from auryn.ingestors.go import resolution
from auryn.ingestors.go.file_parser import parse_source
from auryn.ingestors.go.http_idioms import package_name, route_of
from auryn.ingestors.go.ids import project_id


def _func(src: str, name: str):
    rf = parse_source(src.encode(), "main.go")
    return next(f for f in rf.functions if f.name == name)


def _routes(src: str, name: str = "routes") -> list[str]:
    return [r.encode() for r in _func(src, name).http_routes]


def _calls(src: str, name: str = "call") -> list[tuple[str, str]]:
    return [(c.method, c.url) for c in _func(src, name).http_calls]


# -- client side -----------------------------------------------------------------------------------

def test_net_http_package_calls_and_new_request():
    src = """package main
import (
    "context"
    "fmt"
    "net/http"
)
func call(ctx context.Context, id int) {
    http.Get("http://catalog/api/items")
    http.Post(`http://catalog:8080/api/catalog/reserve`, "application/json", nil)
    http.PostForm("/api/form", nil)
    http.Head(fmt.Sprintf("http://catalog/api/items/%d", id))
    http.NewRequest("DELETE", "/api/items/" + fmt.Sprint(id), nil)
    http.NewRequestWithContext(ctx, http.MethodPut, "https://stock/api/stock", nil)
    http.Get("http://catalog/api/items")
}
"""
    assert _calls(src) == [
        ("GET", "http://catalog/api/items"),
        ("POST", "http://catalog:8080/api/catalog/reserve"),
        ("POST", "/api/form"),
        ("HEAD", "http://catalog/api/items/{param}"),
        ("DELETE", "/api/items/{param}"),
        ("PUT", "https://stock/api/stock"),
    ]


def test_client_values_are_recognized_and_other_receivers_are_not():
    src = """package main
import "net/http"
type cache struct{}
func call(c *http.Client, store *cache, url string) {
    c.Get("/api/from-param")
    local := &http.Client{}
    local.Post("/api/from-local", "", nil)
    http.DefaultClient.Head("/api/default-client")
    store.Get("/users/1")
    http.Get(url)
    http.Get("not-a-route")
}
"""
    assert _calls(src) == [("GET", "/api/from-param"), ("POST", "/api/from-local"),
                           ("HEAD", "/api/default-client")]


def test_an_aliased_net_http_import_still_counts_and_a_file_without_it_records_nothing():
    aliased = 'package main\nimport h "net/http"\nfunc call() { h.Get("/api/x") }\n'
    assert _calls(aliased) == [("GET", "/api/x")]
    no_import = 'package main\nfunc call() { http.Get("/api/x") }\n'
    assert _calls(no_import) == []


def test_resolution_emits_calls_api_to_the_path_keyed_placeholder():
    src = """package main
import "net/http"
func Reserve() {
    http.Post("http://catalog:8080/api/catalog/Reserve/", "application/json", nil)
}
"""
    proj = project_id("shop")
    items = list(resolution.resolve([parse_source(src.encode(), "main.go")], proj))
    key = http_seam.endpoint_placeholder_key("POST", "/api/catalog/reserve")
    node = next(i for i in items if type(i).__name__ == "GraphNode" and i.key["id"] == key)
    assert node.properties == {"name": "POST /api/catalog/reserve", "route": "/api/catalog/reserve",
                               "http_method": "POST", "resolved": False, "provenance": "http-derived"}
    edge = next(i for i in items if type(i).__name__ == "GraphEdge" and i.rel_type == "CALLS_API")
    assert edge.to_key == {"id": key}
    assert edge.from_key["id"].endswith(".func.Reserve()")
    # the ordinary external call placeholder stays alongside it
    assert any(type(i).__name__ == "GraphEdge" and i.rel_type == "CALLS"
               and i.to_key["id"] == "extern://go/net/http.Post" for i in items)


def test_strip_origin_keeps_only_the_path():
    assert http_seam.strip_origin("http://catalog:8080/api/x?y=1") == "/api/x?y=1"
    assert http_seam.strip_origin("https://{param}/api/x") == "/api/x"
    assert http_seam.strip_origin("http://catalog") == "/"
    assert http_seam.strip_origin("/api/x") == "/api/x"
    assert http_seam.strip_origin("ftp://host/x") == "ftp://host/x"


# -- server side -----------------------------------------------------------------------------------

def test_net_http_patterns_and_handler_shapes():
    src = """package main
import (
    "net/http"
    "example.com/shop/handlers"
)
type Server struct{}
func (s *Server) routes(mux *http.ServeMux, h *handlers.Orders) {
    mux.HandleFunc("POST /api/orders/{id}", s.createOrder)
    mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {})
    mux.Handle("GET api.example.com/api/items/{rest...}", http.HandlerFunc(handlers.ListItems))
    mux.Handle("/api/stats/{$}", &StatsHandler{})
    mux.Handle("/api/static/", http.StripPrefix("/api/static/", Files))
    mux.HandleFunc("/api/cancel", h.Cancel)
    http.HandleFunc("/legacy", Legacy)
    mux.Handle("/api/built", handlers.New())
}
"""
    assert _routes(src) == [
        "POST /api/orders/{id} method:Server.createOrder net/http",
        "ANY /health inline net/http",
        "GET /api/items/{rest} func:example.com/shop/handlers.ListItems net/http",
        "ANY /api/stats method:StatsHandler.ServeHTTP net/http",
        "ANY /api/static func:Files net/http",
        "ANY /api/cancel method:example.com/shop/handlers.Orders.Cancel net/http",
        "ANY /legacy func:Legacy net/http",
    ]  # handlers.New() is a constructor call: its handler type is unknown, so no route


def test_gin_groups_middleware_and_params():
    src = """package main
import "github.com/gin-gonic/gin"
func routes() {
    r := gin.Default()
    api := r.Group("/api")
    v1 := api.Group("/v1")
    {
        v1.POST("/orders", auth, CreateOrder)
        v1.GET("/orders/:id", GetOrder)
        v1.GET("", Index)
    }
    r.Group("/admin").DELETE("/files/*path", DeleteFile)
    r.Handle("PATCH", "/x", PatchX)
    r.Any("/any", AnyHandler)
}
"""
    assert _routes(src) == [
        "POST /api/v1/orders func:CreateOrder gin",
        "GET /api/v1/orders/{id} func:GetOrder gin",
        "GET /api/v1 func:Index gin",
        "DELETE /admin/files/{path} func:DeleteFile gin",
        "PATCH /x func:PatchX gin",
        "ANY /any func:AnyHandler gin",
    ]


def test_echo_takes_the_first_handler_and_a_versioned_import():
    src = """package main
import "github.com/labstack/echo/v4"
func routes(h *OrderHandler) {
    e := echo.New()
    g := e.Group("/api")
    g.POST("/orders", h.Create, mw)
    e.Add("PUT", "/api/orders/:id", h.Update)
}
"""
    assert _routes(src) == ["POST /api/orders method:OrderHandler.Create echo",
                            "PUT /api/orders/{id} method:OrderHandler.Update echo"]


def test_chi_route_closures_and_regex_params():
    src = """package main
import (
    "net/http"
    "github.com/go-chi/chi/v5"
)
func routes() {
    r := chi.NewRouter()
    r.Route("/api", func(r chi.Router) {
        r.Post("/orders", CreateOrder)
        r.Route("/orders/{id:[0-9]+}", func(sub chi.Router) {
            sub.Get("/", GetOrder)
        })
        r.With(auth).Delete("/orders/{id}", DeleteOrder)
    })
    r.Group(func(r chi.Router) { r.Method("PATCH", "/x", PatchX) })
    r.Get("/health", Health)
    http.Get("/api/not-a-route")
}
"""
    assert _routes(src) == [
        "POST /api/orders func:CreateOrder chi",
        "GET /api/orders/{id} func:GetOrder chi",
        "DELETE /api/orders/{id} func:DeleteOrder chi",
        "PATCH /x func:PatchX chi",
        "GET /health func:Health chi",
    ]


def test_gorilla_methods_and_subrouter():
    src = """package main
import (
    "net/http"
    "github.com/gorilla/mux"
)
func routes() {
    r := mux.NewRouter()
    r.HandleFunc("/api/orders", CreateOrder).Methods("POST", http.MethodPut)
    s := r.PathPrefix("/api/v2").Subrouter()
    s.HandleFunc("/items/{id}", GetItem).Methods("GET")
    r.HandleFunc("/any", AnyHandler)
}
"""
    assert _routes(src) == [
        "POST /api/orders func:CreateOrder gorilla",
        "PUT /api/orders func:CreateOrder gorilla",
        "GET /api/v2/items/{id} func:GetItem gorilla",
        "ANY /any func:AnyHandler gorilla",
    ]


def test_a_router_from_a_parameter_keeps_its_written_path():
    # Its group prefix is set up by the caller, out of this function's sight (a documented v1 limit).
    src = """package main
import "github.com/gin-gonic/gin"
func routes(g *gin.RouterGroup) { g.POST("/orders", CreateOrder) }
"""
    assert _routes(src) == ["POST /orders func:CreateOrder gin"]


def test_no_routes_without_a_router_import_and_resolution_stores_the_property():
    plain = 'package main\nfunc routes() { r.GET("/x", H); m.HandleFunc("/y", H) }\n'
    assert _routes(plain) == []
    src = 'package main\nimport "net/http"\nfunc routes() { http.HandleFunc("/x", H) }\nfunc H() {}\n'
    items = list(resolution.resolve([parse_source(src.encode(), "main.go")], project_id("shop")))
    props = {i.properties["name"]: i.properties.get("http_routes")
             for i in items if type(i).__name__ == "GraphNode" and i.label == "Function"
             and i.key["id"].startswith("go://")}
    assert props == {"routes": ["ANY /x func:H net/http"], "H": None}


def test_helpers():
    assert package_name("github.com/go-chi/chi/v5") == "chi"
    assert package_name("gopkg.in/yaml.v3") == "yaml"
    assert package_name("net/http") == "http"
    assert route_of("api/v1", "/orders/:id/") == "/api/v1/orders/{id}"
    assert route_of("", "/") == "/"
