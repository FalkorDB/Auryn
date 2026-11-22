"""Unit tests for the project-wide Go resolution pass (no FalkorDB): RawFile list -> graph
nodes/edges. Exercises the FLAT package tier (no HAS_SUBPACKAGE), method attachment to the
receiver type, EMBEDS (struct + interface embedding), DEPENDS_ON (param/return, field, cross-
package qualified, construction), CALLS resolution (bare same-package func, receiver/param/
package-var-typed method, package-qualified cross-package func), and the deliberate absence of
IMPLEMENTS."""

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


def test_call_on_package_var_typed_receiver():
    nodes, edges = _resolve({
        "bus/bus.go": (
            "package bus\n"
            'import (\n"net/http"\nnats "github.com/nats-io/nats.go"\n)\n'
            "type Cache interface { Put(k string) }\n"
            "var nc *nats.Conn\n"
            "var (\nclient *http.Client\ncache Cache\n)\n"
            'func Send() { nc.Publish("orders", nil); client.Do(nil); cache.Put("k") }\n'
        ),
    })
    calls = _edges(edges, "CALLS")
    nats_publish = "extern://go/github.com/nats-io/nats.go.Conn.Publish"
    assert calls[("bus.func.Send()", nats_publish)] == {"resolution": "external"}
    assert _node_by_id(nodes, nats_publish).properties["origin"] == "external"
    assert _node_by_id(nodes, "extern://go/net/http.Client.Do").properties["origin"] == "stdlib"
    assert calls[("bus.func.Send()", "bus.interface.Cache.method.Put(string)")] == {"resolution": "name"}


def test_param_shadows_a_package_var():
    _, edges = _resolve({
        "bus/bus.go": (
            "package bus\n"
            'import (\n"net/http"\n"sync"\n)\n'
            "var c *http.Client\n"
            "func Send(c *sync.Mutex) { c.Lock() }\n"
        ),
    })
    assert {t for (_f, t) in _edges(edges, "CALLS")} == {"extern://go/sync.Mutex.Lock"}


def test_package_var_of_a_sibling_file_is_not_used():
    # Only the func's own file types its package vars: an incremental resync re-parses just the
    # files it re-emits, so a var from an untouched sibling file would resolve differently there.
    _, edges = _resolve({
        "bus/lock.go": 'package bus\nimport "sync"\nvar mu *sync.Mutex\n',
        "bus/send.go": 'package bus\nimport "sync"\nfunc Send(wg *sync.WaitGroup) { mu.Lock(); wg.Done() }\n',
    })
    assert {t for (_f, t) in _edges(edges, "CALLS")} == {"extern://go/sync.WaitGroup.Done"}


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
