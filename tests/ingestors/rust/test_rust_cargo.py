"""Cargo awareness (issue #5): the path-derived crate/target identity, manifest parsing, and the
end-to-end workspace behavior -- two crates with the same module path + function name stay DISTINCT
(the bug this fixes), lib vs bin targets are modeled, and `[dependencies]` become Package DEPENDS_ON
Package edges. The pure-function tests need no FalkorDB; the ingest tests use the `graph` fixture."""

from __future__ import annotations

import textwrap

from auryn.core import ingest
from auryn.core.graph_client import GraphWriter
from auryn.ingestors.rust import cargo


# -- pure: path-derived crate/target identity ----------------------------------------------

def test_crate_target_for_path_layout():
    def ct(p):
        t = cargo.crate_target_for_path(p)
        return (t.package_dir, t.kind, t.name)

    assert ct("crates/api/src/util.rs") == ("crates/api", "lib", "api")
    assert ct("crates/api/src/lib.rs") == ("crates/api", "lib", "api")
    assert ct("crates/api/src/main.rs") == ("crates/api", "bin", "api")        # default binary
    assert ct("crates/api/src/bin/tool.rs") == ("crates/api", "bin", "tool")
    assert ct("crates/api/src/bin/tool/helpers.rs") == ("crates/api", "bin", "tool")
    assert ct("crates/api/tests/it.rs") == ("crates/api", "test", "it")
    assert ct("crates/api/examples/demo.rs") == ("crates/api", "example", "demo")
    assert ct("crates/api/benches/b.rs") == ("crates/api", "bench", "b")
    assert ct("src/lib.rs") == ("", "lib", "crate")                            # repo-root crate
    assert ct("build.rs") == ("", "build-script", "build")


def test_crate_target_hyphen_normalized():
    # Cargo normalizes a package-dir hyphen to an underscore in the default crate name.
    assert cargo.crate_target_for_path("crates/my-api/src/lib.rs").name == "my_api"


# -- pure: manifest parsing + model --------------------------------------------------------

def test_parse_manifest_and_model_dependencies():
    api = cargo.parse_manifest("crates/api", textwrap.dedent("""
        [package]
        name = "api"
        version = "0.3.0"
        edition = "2021"
        [dependencies]
        core = { path = "../core" }
        serde = "1.0"
        [dev-dependencies]
        proptest = "1"
    """).encode())
    core = cargo.parse_manifest("crates/core", b'[package]\nname = "core"\nedition = "2018"\n')

    assert api.name == "api" and api.version == "0.3.0" and api.edition == "2021"
    model = cargo.build_cargo_model([api, core])
    # api -> core is an intra-repo edge (by path); serde is external; proptest (dev) is excluded.
    assert ("crates/api", "crates/core") in model.depends_on
    api_pkg = next(p for p in model.packages if p.name == "api")
    assert api_pkg.external_dependencies == ["serde"]
    assert model.depends_on == [("crates/api", "crates/core")]  # the only runtime intra-repo edge


def test_workspace_edition_inheritance():
    root = cargo.parse_manifest("", b'[workspace]\nmembers=["a"]\n[workspace.package]\nedition="2021"\n')
    member = cargo.parse_manifest("a", b'[package]\nname="a"\nedition.workspace=true\n')
    model = cargo.build_cargo_model([root, member])
    assert next(p for p in model.packages if p.name == "a").edition == "2021"


# -- end-to-end: a two-crate workspace -----------------------------------------------------

def _workspace(root):
    """Write a minimal Cargo workspace: crate `api` (lib + default binary) depends on crate `core`;
    BOTH define `util::parse` -- the exact collision issue #5 is about."""
    (root / "Cargo.toml").write_text('[workspace]\nmembers = ["crates/*"]\n')
    for name, extra in (("api", 'core = { path = "../core" }\n'), ("core", "")):
        d = root / "crates" / name
        (d / "src").mkdir(parents=True)
        (d / "Cargo.toml").write_text(
            f'[package]\nname = "{name}"\nversion = "0.1.0"\nedition = "2021"\n'
            f'[dependencies]\n{extra}'
        )
        (d / "src" / "lib.rs").write_text("pub mod util { pub fn parse(s: &str) -> usize { s.len() } }\n")
    # api also has a binary crate (src/main.rs) -> a distinct bin target.
    (root / "crates" / "api" / "src" / "main.rs").write_text("fn main() { println!(\"hi\"); }\n")
    return root


def _ingest_ws(graph, root):
    ingest.ingest_directory(GraphWriter(graph), str(root), project_name="ws")
    return lambda cypher: graph.query(cypher).result_set


def test_same_name_in_two_crates_does_not_collide(graph, tmp_path):
    q = _ingest_ws(graph, _workspace(tmp_path))
    # The bug: without crate awareness the two `util::parse` collapse to ONE node. Now there are two,
    # each under its own crate's `util` module.
    owners = {r[0] for r in q(
        "MATCH (c:Crate)-[:HAS_MODULE]->(:Module {name:'util'})-[:HAS_FUNCTION]->(:Function {name:'parse'}) "
        "RETURN c.name")}
    assert owners == {"api", "core"}
    assert q("MATCH (f:Function {name:'parse'}) RETURN count(f)")[0][0] == 2  # two distinct nodes


def test_lib_and_bin_targets(graph, tmp_path):
    q = _ingest_ws(graph, _workspace(tmp_path))
    kinds = {(r[0], r[1]) for r in q("MATCH (p:Package)-[:HAS_TARGET]->(c:Crate) RETURN p.name, c.kind")}
    assert ("api", "lib") in kinds and ("api", "bin") in kinds and ("core", "lib") in kinds


def test_crate_dependency_edge_and_package_metadata(graph, tmp_path):
    q = _ingest_ws(graph, _workspace(tmp_path))
    # [dependencies] core = { path = ".." } -> Package api DEPENDS_ON Package core.
    assert q("MATCH (:Package {name:'api'})-[r:DEPENDS_ON]->(:Package {name:'core'}) "
             "RETURN r.resolution") == [["name"]]
    # the reverse edge must NOT exist (core does not depend on api)
    assert q("MATCH (:Package {name:'core'})-[:DEPENDS_ON]->(:Package {name:'api'}) RETURN 1") == []
    # manifest metadata reconciled onto the Package node
    assert q("MATCH (p:Package {name:'api'}) RETURN p.edition, p.version") == [["2021", "0.1.0"]]
