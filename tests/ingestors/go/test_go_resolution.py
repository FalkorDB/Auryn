"""Unit tests for the project-wide Go resolution pass (no FalkorDB): RawFile list -> graph
nodes/edges. Exercises the FLAT package tier (no HAS_SUBPACKAGE), method attachment to the
receiver type, EMBEDS (struct + interface embedding), DEPENDS_ON (param/return, field, cross-
package qualified, construction), CALLS resolution (bare same-package func, receiver/param-typed
method, package-qualified cross-package func), and the deliberate absence of IMPLEMENTS."""

from __future__ import annotations

from auryn.ingestors.go import resolution
from auryn.ingestors.go.file_parser import parse_source
from auryn.ingestors.go.ids import project_id

_PROJ = project_id("demo")


def _resolve(files: dict[str, str]):
    # No go.mod here (resolution unit tests): a package's import path falls back to its directory, so
    # the cross-package fixtures import by the bare dir name (`import "model"`). The go.mod/Module
    # tier + import-path linkage are covered in test_go_gomod.py and test_go_git_sync.py.
    raw = [parse_source(src.encode(), path) for path, src in files.items()]
    items = list(resolution.resolve(raw, _PROJ))
    nodes = [i for i in items if type(i).__name__ == "GraphNode"]
    edges = [i for i in items if type(i).__name__ == "GraphEdge"]
    return nodes, edges


def _edges(edges, rel):
    def tail(k):
        return k["id"].replace(_PROJ + "/package/", "").replace(_PROJ, "~")
    return {(tail(e.from_key), tail(e.to_key)): (e.properties or {}) for e in edges if e.rel_type == rel}


def _node_by_id(nodes, node_id):
    return next((n for n in nodes if n.key["id"] == node_id), None)


def _by_name(nodes, label, name):
    return next(n for n in nodes if n.label == label and n.properties.get("name") == name)


def test_package_tier_is_flat():
    _, edges = _resolve({
        "model/a.go": "package model\ntype A struct {}",
        "app/b.go": "package app\ntype B struct {}",
    })
    assert _edges(edges, "HAS_PACKAGE") == {("~", "model"): {}, ("~", "app"): {}}
    # Go package names are not hierarchical -> no HAS_SUBPACKAGE edge is ever emitted.
    assert all(e.rel_type != "HAS_SUBPACKAGE" for e in edges)


def test_types_functions_methods_containment():
    _, edges = _resolve({
        "model/store.go": (
            "package model\n"
            "type Store struct { items []string }\n"
            "func New() *Store { return nil }\n"
            "func (s *Store) Put(v string) error { return nil }\n"
        )
    })
    assert ("model", "model.struct.Store") in _edges(edges, "HAS_TYPE")
    assert ("model", "model.func.New()") in _edges(edges, "HAS_FUNCTION")
    assert ("model.struct.Store", "model.struct.Store.method.Put(string)") in _edges(edges, "HAS_METHOD")


def test_struct_and_interface_embeds():
    _, edges = _resolve({
        "model/reader.go": "package model\ntype Reader interface { Read() string }",
        "model/repo.go": "package model\ntype Repository interface { Reader; Put(v string) error }",
        "model/store.go": "package model\ntype Base struct { id int }\ntype Store struct { Base; items []string }",
    })
    emb = _edges(edges, "EMBEDS")
    assert ("model.struct.Store", "model.struct.Base") in emb          # struct embedding
    assert ("model.interface.Repository", "model.interface.Reader") in emb  # interface embedding


def test_depends_on_param_field_and_construction():
    _, edges = _resolve({
        "model/store.go": (
            "package model\n"
            "type ID = int\n"
            "type Store struct { id ID }\n"
            "func New() *Store { return &Store{} }\n"
            "func Make(other *Store) *Store { return other }\n"
        )
    })
    dep = _edges(edges, "DEPENDS_ON")
    assert ("model.struct.Store.field.id", "model.type.ID") in dep       # field type
    assert ("model.func.New()", "model.struct.Store") in dep            # construction / return
    assert ("model.func.Make(*Store)", "model.struct.Store") in dep     # parameter type


def test_cross_package_depends_and_calls():
    _, edges = _resolve({
        "model/store.go": "package model\ntype Store struct {}\nfunc New() *Store { return &Store{} }",
        "app/service.go": (
            "package app\n"
            'import "model"\n'
            "type Service struct { store *model.Store }\n"
            "func NewService() *Service { return &Service{store: model.New()} }\n"
        ),
    })
    dep = _edges(edges, "DEPENDS_ON")
    assert ("app.struct.Service.field.store", "model.struct.Store") in dep  # qualified cross-pkg field
    calls = _edges(edges, "CALLS")
    key = ("app.func.NewService()", "model.func.New()")
    assert key in calls and calls[key] == {"resolution": "name"}            # package-qualified call


def test_calls_bare_and_receiver_typed():
    _, edges = _resolve({
        "model/store.go": (
            "package model\n"
            "type Store struct { items []string }\n"
            "func (s *Store) Put(v string) error { s.record(v); return nil }\n"
            "func (s *Store) record(v string) {}\n"
            "func Run() { helper() }\n"
            "func helper() {}\n"
        )
    })
    calls = _edges(edges, "CALLS")
    # receiver-typed: Put -> record (via the receiver s, of type *Store)
    assert ("model.struct.Store.method.Put(string)", "model.struct.Store.method.record(string)") in calls
    # bare same-package: Run -> helper
    assert ("model.func.Run()", "model.func.helper()") in calls


def test_call_on_param_typed_interface():
    _, edges = _resolve({
        "model/x.go": (
            "package model\n"
            "type Repository interface { Size() int }\n"
            "func Use(r Repository) int { return r.Size() }\n"
        )
    })
    calls = _edges(edges, "CALLS")
    assert ("model.func.Use(Repository)", "model.interface.Repository.method.Size()") in calls


def test_no_implements_edge_emitted():
    _, edges = _resolve({
        "model/repo.go": "package model\ntype Repository interface { Put(v string) error }",
        "model/store.go": "package model\ntype Store struct {}\nfunc (s *Store) Put(v string) error { return nil }",
    })
    # Store structurally satisfies Repository, but structural satisfaction is deep-pass work.
    assert all(e.rel_type != "IMPLEMENTS" for e in edges)


# -- ADR 0004: external references as upgradable placeholder nodes ------------------------

def test_stdlib_call_becomes_placeholder():
    # A `fmt.Println` call (fmt = imported stdlib package, not a project package) is no longer
    # dropped: it surfaces as a first-class Function placeholder reached by an ordinary CALLS edge.
    nodes, edges = _resolve({
        "app/main.go": (
            "package app\n"
            'import "fmt"\n'
            'func Run() { fmt.Println("hi") }\n'
        ),
    })
    eid = "extern://go/fmt.Println"
    ph = _node_by_id(nodes, eid)
    assert ph is not None and ph.label == "Function"
    assert ph.properties["name"] == "Println"
    assert ph.properties["qualified_name"] == "fmt.Println"
    assert ph.properties["resolved"] is False
    assert ph.properties["origin"] == "stdlib"
    assert ph.properties["linkage_key"] == eid
    calls = _edges(edges, "CALLS")
    assert calls[("app.func.Run()", eid)] == {"resolution": "external"}


def test_third_party_call_has_external_origin():
    # An imported non-stdlib, non-project package -> origin "external" (the candidate a future
    # ingest of that repo upgrades in place).
    nodes, _ = _resolve({
        "app/main.go": (
            "package app\n"
            'import "github.com/pkg/errors"\n'
            'func Run() { errors.Wrap(nil, "x") }\n'
        ),
    })
    # keyed by the FULL import path the source writes, so a later ingest of that module heals onto
    # it (not the bare `errors.Wrap` last-segment proxy).
    ph = _node_by_id(nodes, "extern://go/github.com/pkg/errors.Wrap")
    assert ph is not None and ph.properties["origin"] == "external"


def test_external_method_call_via_typed_param():
    # A method call on a param whose static type is an imported non-project `pkg.Type`
    # (`io.Reader`) -> a Method placeholder keyed `pkg.Type.Method`.
    nodes, edges = _resolve({
        "app/use.go": (
            "package app\n"
            'import "io"\n'
            "func Use(r io.Reader) { r.Read(nil) }\n"
        ),
    })
    eid = "extern://go/io.Reader.Read"
    ph = _node_by_id(nodes, eid)
    assert ph is not None and ph.label == "Method"
    assert ph.properties["qualified_name"] == "io.Reader.Read"
    assert ph.properties["resolved"] is False
    assert ph.properties["origin"] == "stdlib"
    calls = _edges(edges, "CALLS")
    assert calls[("app.func.Use(io.Reader)", eid)] == {"resolution": "external"}


def _call_targets(edges, caller):
    return {t for (f, t) in _edges(edges, "CALLS") if f == caller}


def test_non_aliased_import_binds_the_package_name():
    # Regression: a non-aliased import was keyed only by its LAST path segment, but Go code writes the
    # package name, so `chi.NewRouter()` under `import "github.com/go-chi/chi/v5"` (keyed `v5`) got no
    # placeholder. Same for every major-versioned module, a gopkg.in `.vN` path, and a `nats.go` /
    # `go-redis` style repo name. The placeholder stays keyed by the FULL import path (ADR 0004).
    nodes, edges = _resolve({
        "app/main.go": (
            "package app\n"
            "import (\n"
            '\t"github.com/go-chi/chi/v5"\n'
            '\t"github.com/nats-io/nats.go"\n'
            '\t"github.com/redis/go-redis/v9"\n'
            '\t"gopkg.in/yaml.v3"\n'
            ")\n"
            "func Run() {\n"
            "\tchi.NewRouter()\n"
            '\tnats.Connect("nats://bus:4222")\n'
            "\tredis.NewClient(nil)\n"
            "\tyaml.Unmarshal(nil, nil)\n"
            "}\n"
        ),
    })
    expected = {
        "extern://go/github.com/go-chi/chi/v5.NewRouter",
        "extern://go/github.com/nats-io/nats.go.Connect",
        "extern://go/github.com/redis/go-redis/v9.NewClient",
        "extern://go/gopkg.in/yaml.v3.Unmarshal",
    }
    assert _call_targets(edges, "app.func.Run()") == expected
    for eid in expected:
        ph = _node_by_id(nodes, eid)
        assert ph.properties["origin"] == "external" and ph.properties["linkage_key"] == eid


def test_versioned_import_types_a_method_call():
    # The same binding names the type of a param, so a method call on it gets a Method placeholder.
    nodes, edges = _resolve({
        "app/db.go": (
            "package app\n"
            'import "github.com/jackc/pgx/v5"\n'
            "func Ping(conn *pgx.Conn) { conn.Ping(nil) }\n"
        ),
    })
    eid = "extern://go/github.com/jackc/pgx/v5.Conn.Ping"
    assert _node_by_id(nodes, eid).label == "Method"
    assert _call_targets(edges, "app.func.Ping(*pgx.Conn)") == {eid}


def test_assumed_package_name_never_displaces_a_stronger_key():
    # The assumed name is a guess (only the imported package's own clause is authoritative):
    # `k8s.io/apimachinery/pkg/apis/meta/v1` declares `package v1`, so its last segment stays bound;
    # the exact last segment of `.../api/meta` keeps `meta` over the `meta` guessed for `.../meta/v1`;
    # an aliased import binds its alias only, and two imports guessing the same name bind neither.
    _, edges = _resolve({
        "app/main.go": (
            "package app\n"
            "import (\n"
            '\t"k8s.io/apimachinery/pkg/api/meta"\n'
            '\t"k8s.io/apimachinery/pkg/apis/meta/v1"\n'
            '\trouter "github.com/go-chi/chi/v5"\n'
            '\t"github.com/go-redis/redis/v8"\n'
            '\t"github.com/redis/go-redis/v9"\n'
            ")\n"
            "func Run() {\n"
            "\tv1.Now()\n"
            "\tmeta.Accessor(nil)\n"
            "\trouter.NewRouter()\n"
            "\tchi.NewMux()\n"
            "\tredis.NewClient(nil)\n"
            "}\n"
        ),
    })
    assert _call_targets(edges, "app.func.Run()") == {
        "extern://go/k8s.io/apimachinery/pkg/apis/meta/v1.Now",
        "extern://go/k8s.io/apimachinery/pkg/api/meta.Accessor",
        "extern://go/github.com/go-chi/chi/v5.NewRouter",
    }


def test_incremental_resolution_binds_the_same_package_name():
    # resolve_incremental re-parses only the emit file and seeds the rest from the graph, which carries
    # no import data (read_go_index), so the binding comes from the emit file alone and must name the
    # same placeholder the full pass does.
    files = {
        "model/store.go": "package model\nfunc New() int { return 0 }\n",
        "app/main.go": (
            "package app\n"
            "import (\n"
            '\t"model"\n'
            '\t"github.com/go-chi/chi/v5"\n'
            ")\n"
            "func Run() { chi.NewRouter(); model.New() }\n"
        ),
    }
    raws = {path: parse_source(src.encode(), path) for path, src in files.items()}
    nodes, full = _resolve(files)
    new_id = _by_name(nodes, "Function", "New").key["id"]
    seed = resolution.GoGraphIndex(project_import_paths={"model"}, func_by_name={"New": [(new_id, "model")]})
    incremental = [i for i in resolution.resolve_incremental([raws["app/main.go"]], _PROJ, seed)
                   if type(i).__name__ == "GraphEdge"]
    expected = {"extern://go/github.com/go-chi/chi/v5.NewRouter", "model.func.New()"}
    assert _call_targets(incremental, "app.func.Run()") == _call_targets(full, "app.func.Run()") == expected


def test_real_callables_carry_linkage_key():
    nodes, _ = _resolve({
        "model/store.go": (
            "package model\n"
            "type Store struct {}\n"
            "func New() *Store { return nil }\n"
            "func (s *Store) Put(v string) error { return nil }\n"
            "func (s *Store) record(v string) {}\n"
        ),
        "model/repo.go": "package model\ntype Repository interface { Size() int }",
    })
    # free function, concrete method, and interface method spec each carry a cross-repo key.
    assert _by_name(nodes, "Function", "New").properties["linkage_key"] == "extern://go/model.New"
    assert _by_name(nodes, "Method", "Put").properties["linkage_key"] == "extern://go/model.Store.Put"
    assert _by_name(nodes, "Method", "Size").properties["linkage_key"] == "extern://go/model.Repository.Size"
    # an unexported method can't be named from another package -> no cross-repo key.
    assert "linkage_key" not in _by_name(nodes, "Method", "record").properties


def test_intra_project_dangling_not_placeholdered():
    # GUARD: unnameable/intra-project unresolved calls stay dropped (no placeholder), so nothing
    # stale can survive a resync. `missing()` is a bare same-package dangler; `model.Gone()`
    # targets a PROJECT package's missing symbol -- both are intra-project, not external.
    nodes, edges = _resolve({
        "model/store.go": "package model\ntype Store struct {}\nfunc New() *Store { return nil }",
        "app/use.go": (
            "package app\n"
            'import "model"\n'
            "func Run() { missing(); model.Gone() }\n"
        ),
    })
    assert not any(n.key["id"].startswith("extern://go/") for n in nodes)
    assert all(e.properties.get("resolution") != "external"
               for e in edges if e.rel_type == "CALLS")


def test_bare_reference_does_not_leak_across_packages():
    # `Store` referenced (unqualified) from package `app` must NOT resolve to model.Store:
    # Go forbids unqualified cross-package references, so no DEPENDS_ON should appear.
    _, edges = _resolve({
        "model/store.go": "package model\ntype Store struct {}",
        "app/use.go": "package app\nfunc Use(s *Store) {}",  # (won't compile in real Go; tests the guard)
    })
    dep = _edges(edges, "DEPENDS_ON")
    assert not any(f.startswith("app.") for (f, _t) in dep)


def test_doc_property_reaches_nodes():
    nodes, _ = _resolve({
        "model/store.go": (
            "package model\n"
            "// Store persists widgets.\n"
            "type Store struct { items []string }\n"
            "// New builds a Store.\n"
            "func New() *Store { return nil }\n"
            "// Put stores a value.\n"
            "func (s *Store) Put(v string) error { return nil }\n"
        ),
    })
    assert _by_name(nodes, "Struct", "Store").properties["doc"] == "Store persists widgets."
    assert _by_name(nodes, "Function", "New").properties["doc"] == "New builds a Store."
    assert _by_name(nodes, "Method", "Put").properties["doc"] == "Put stores a value."
