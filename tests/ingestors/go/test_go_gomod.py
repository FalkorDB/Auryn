"""go.mod awareness + the import-path package tier (the Go analog of the Rust crate work): packages
are keyed by DIRECTORY so two dirs sharing a `package` clause name stay distinct (the collision this
fixes), a go.mod Module tier sits above them, and cross-repo linkage keys are IMPORT-PATH-qualified
so healing locates the right newly-discovered symbol -- even when the package name differs from the
import path's last segment (`import "github.com/x/go-foo"` + `package foo`). Pure tests need no
FalkorDB; the ingest tests use the `graph` fixture."""

from __future__ import annotations

from auryn.core import ingest
from auryn.core.graph_client import GraphWriter
from auryn.ingestors.go import gomod, ids


# -- pure: go.mod parsing + import-path derivation -----------------------------------------

def test_parse_gomod_module_go_and_requires():
    m = gomod.parse_gomod("", b"module github.com/acme/widgets\n\ngo 1.22\n\n"
                              b"require (\n\tgithub.com/pkg/errors v0.9.1\n\tgolang.org/x/sync v0.5.0 // indirect\n)\n"
                              b"\nrequire github.com/stretchr/testify v1.9.0\n")
    assert m.module_path == "github.com/acme/widgets" and m.go_version == "1.22"
    reqs = {(r.module_path, r.indirect) for r in m.requires}
    assert reqs == {("github.com/pkg/errors", False), ("golang.org/x/sync", True),
                    ("github.com/stretchr/testify", False)}


def test_import_path_for_dir_and_owning_module():
    mm = gomod.build_module_map([
        gomod.parse_gomod("", b"module github.com/acme/root\n"),
        gomod.parse_gomod("services/api", b"module github.com/acme/api\n"),
    ])
    assert gomod.import_path_for_dir("store", mm) == "github.com/acme/root/store"
    assert gomod.import_path_for_dir("services/api/handlers", mm) == "github.com/acme/api/handlers"
    assert gomod.import_path_for_dir("services/api", mm) == "github.com/acme/api"   # nested wins
    assert gomod.owning_module_dir("services/api/handlers", mm.keys()) == "services/api"
    assert gomod.import_path_for_dir("x", {}) == "x"  # no go.mod -> repo-relative dir


# -- end-to-end --------------------------------------------------------------------------

def _write(root, files):
    for rel, src in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    return root


def _q(graph, root, project="ws"):
    ingest.ingest_directory(GraphWriter(graph), str(root), project_name=project)
    return lambda cypher: graph.query(cypher).result_set


def test_same_package_name_in_two_dirs_does_not_collide(graph, tmp_path):
    # The bug: two directories both `package util`, each with a `Parse` func. Keyed by the bare
    # clause name they collapsed to ONE package/func; keyed by directory they stay distinct.
    q = _q(graph, _write(tmp_path, {
        "go.mod": "module demo\n\ngo 1.22\n",
        "a/util/u.go": "package util\n\nfunc Parse(s string) int { return len(s) }\n",
        "b/util/u.go": "package util\n\nfunc Parse(s string) int { return len(s) }\n",
    }))
    pkgs = {r[0] for r in q("MATCH (p:Package {name:'util'}) RETURN p.import_path")}
    assert pkgs == {"demo/a/util", "demo/b/util"}            # two distinct packages
    assert q("MATCH (f:Function {name:'Parse'}) RETURN count(f)")[0][0] == 2  # two distinct funcs


def test_external_test_package_distinct_from_production(graph, tmp_path):
    # Go allows `package calc` and the black-box external `package calc_test` in ONE directory --
    # distinct compilation units. Dir-only keying would merge their same-named helpers into one node.
    q = _q(graph, _write(tmp_path, {
        "go.mod": "module demo\n\ngo 1.22\n",
        "calc/calc.go": "package calc\n\nfunc Helper() int { return 1 }\n",
        "calc/calc_ext_test.go": "package calc_test\n\nfunc Helper() int { return 2 }\n",
    }))
    names = {r[0] for r in q("MATCH (p:Package) WHERE p.name IN ['calc', 'calc_test'] RETURN p.name")}
    assert names == {"calc", "calc_test"}                                        # two distinct packages
    assert q("MATCH (f:Function {name:'Helper'}) RETURN count(f)")[0][0] == 2     # two distinct funcs


def test_qualified_field_type_resolves_via_defining_file_import(graph, tmp_path):
    # Two packages share the clause name `util` (a/util and b/util). A consumer whose field is typed
    # util.Base and which imports demo/a/util must resolve the field DEPENDS_ON to a/util's Base --
    # the importer's path is unambiguous even though the name isn't globally unique.
    q = _q(graph, _write(tmp_path, {
        "go.mod": "module demo\n\ngo 1.22\n",
        "a/util/base.go": "package util\n\ntype Base struct { n int }\n",
        "b/util/base.go": "package util\n\ntype Base struct { n int }\n",
        "consumer/c.go": 'package consumer\n\nimport "demo/a/util"\n\ntype Widget struct { base util.Base }\n',
    }))
    rows = q("MATCH (:Field {name:'base'})-[:DEPENDS_ON]->(t:Struct {name:'Base'}), "
             "(p:Package)-[:HAS_TYPE]->(t) RETURN p.import_path")
    assert rows == [["demo/a/util"]]  # resolved to the imported package, not b/util, and not dropped


def test_module_tier_over_dir_keyed_packages(graph, tmp_path):
    q = _q(graph, _write(tmp_path, {
        "go.mod": "module demo\n\ngo 1.22\n",
        "store/store.go": "package store\n\nfunc Save() {}\n",
    }))
    # Project -> Module(demo) -> Package(store, import_path demo/store).
    assert q("MATCH (:Project)-[:HAS_MODULE]->(m:Module) RETURN m.name, m.go_version") == [["demo", "1.22"]]
    assert q("MATCH (:Module {name:'demo'})-[:HAS_PACKAGE]->(p:Package {name:'store'}) "
             "RETURN p.import_path") == [["demo/store"]]
    # the store package's exported func carries the import-path-qualified cross-repo key.
    assert q("MATCH (f:Function {name:'Save'}) RETURN f.linkage_key") == [["extern://go/demo/store.Save"]]


def test_written_external_import_is_authoritative(graph, tmp_path):
    # module demo has a LOCAL `store` package, but app.go imports the EXTERNAL
    # github.com/vendor/store (same last segment "store"). store.Save() must resolve to the
    # EXTERNAL package (a placeholder), never fall back to the local demo/store.Save.
    q = _q(graph, _write(tmp_path, {
        "go.mod": "module demo\n\ngo 1.22\n",
        "store/store.go": "package store\n\nfunc Save() {}\n",
        "app/app.go": 'package app\n\nimport "github.com/vendor/store"\n\nfunc Use() { store.Save() }\n',
    }))
    assert q("MATCH (:Function {name:'Use'})-[:CALLS]->(p {resolved:false}) RETURN p.id") == \
        [["extern://go/github.com/vendor/store.Save"]]
    # the LOCAL Save received no call from Use (it was not wrongly matched by last-segment)
    assert q("MATCH (:Function {name:'Use'})-[:CALLS]->(f:Function {name:'Save'}) "
             "WHERE f.resolved IS NULL RETURN count(f)") == [[0]]


def test_cross_repo_heal_with_path_name_divergence(graph, tmp_path):
    # The case the bare-name scheme got wrong: the import path's last segment (`go-foo`) differs
    # from the name the source calls through (`foo`, bound by the explicit import alias). Repo A
    # imports the FULL path and calls foo.Bar; ingesting repo B (which DEFINES it, at module
    # github.com/x/go-foo) must HEAL A's placeholder onto B's real Bar via the import-path key.
    repo_a = _write(tmp_path / "a", {
        "go.mod": "module github.com/x/app\n\ngo 1.22\n",
        "main.go": 'package main\n\nimport foo "github.com/x/go-foo"\n\n'
                   "func run() int { return foo.Bar() }\n\nfunc main() { run() }\n",
    })
    repo_b = _write(tmp_path / "b", {
        "go.mod": "module github.com/x/go-foo\n\ngo 1.22\n",
        "foo.go": "package foo\n\nfunc Bar() int { return 7 }\n",
    })

    w = GraphWriter(graph)
    ingest.ingest_directory(w, str(repo_a), project_name="app")   # A first: Bar is a placeholder
    key = "extern://go/github.com/x/go-foo.Bar"
    assert graph.query(f"MATCH (p {{id:'{key}', resolved:false}}) RETURN 1").result_set == [[1]]

    ingest.ingest_directory(w, str(repo_b), project_name="foo")   # B lands -> heal
    # A's run() now CALLS B's real Bar (resolved), and the placeholder is gone.
    assert graph.query(
        "MATCH (:Function {name:'run'})-[c:CALLS]->(b:Function {name:'Bar'}) "
        "WHERE b.resolved IS NULL RETURN c.resolution").result_set == [["resolved"]]
    assert graph.query(f"MATCH (p {{id:'{key}'}}) RETURN count(p)").result_set == [[0]]
