"""Unit tests for the project-wide Bash resolution pass (no FalkorDB): RawFile list -> graph
nodes/edges. Exercises the flat model (File -> Function via HAS_FUNCTION, no package/type tier),
CALLS resolution (local function = resolved/"name", external command = placeholder/"external"),
`source`/`.` includes as IMPORTS (resolved to a project file vs an external placeholder file),
the ADR 0004 cross-repo `linkage_key`, and the deliberate absence of any type/Method/Parameter
node or IMPLEMENTS/DEPENDS_ON edge."""

from __future__ import annotations

from auryn.ingestors.bash import ids, resolution
from auryn.ingestors.bash.file_parser import parse_source
from auryn.ingestors.bash.ids import project_id

_PROJ = project_id("demo")


def _resolve(files: dict[str, str]):
    raw = [parse_source(src.encode(), path) for path, src in files.items()]
    items = list(resolution.resolve(raw, _PROJ))
    nodes = [i for i in items if type(i).__name__ == "GraphNode"]
    edges = [i for i in items if type(i).__name__ == "GraphEdge"]
    return nodes, edges


def _tail(k):
    return k["id"].replace(_PROJ + "/file/", "").replace(_PROJ, "~")


def _edges(edges, rel):
    return {(_tail(e.from_key), _tail(e.to_key)): (e.properties or {}) for e in edges if e.rel_type == rel}


def _node_by_id(nodes, node_id):
    return next((n for n in nodes if n.key["id"] == node_id), None)


def _by_name(nodes, label, name):
    return next(n for n in nodes if n.label == label and n.properties.get("name") == name)


def test_files_and_function_containment():
    nodes, edges = _resolve({
        "lib.sh": "greet() { echo hi; }\nfunction log { printf x; }",
    })
    assert _edges(edges, "HAS_FILE") == {("~", "lib.sh"): {}}
    has_fn = _edges(edges, "HAS_FUNCTION")
    assert ("lib.sh", "lib.sh.func.greet") in has_fn
    assert ("lib.sh", "lib.sh.func.log") in has_fn
    # no invented tiers: bash has no packages/types/methods/parameters.
    for absent in ("HAS_PACKAGE", "HAS_TYPE", "HAS_METHOD", "HAS_PARAMETER", "HAS_FIELD"):
        assert all(e.rel_type != absent for e in edges)
    assert all(n.label not in ("Package", "Struct", "Interface", "Method", "Parameter", "TypeAlias") for n in nodes)


def test_function_to_function_call_resolved():
    _, edges = _resolve({
        "app.sh": (
            "main() { helper; }\n"
            "helper() { echo done; }\n"
        )
    })
    calls = _edges(edges, "CALLS")
    key = ("app.sh.func.main", "app.sh.func.helper")
    assert key in calls and calls[key] == {"resolution": "name"}


def test_cross_file_call_resolves_by_bare_name():
    # bash functions are global once sourced -> a call resolves to a project function defined in
    # another file by bare name.
    _, edges = _resolve({
        "lib.sh": "shared() { echo hi; }",
        "main.sh": "run() { shared; }",
    })
    calls = _edges(edges, "CALLS")
    assert ("main.sh.func.run", "lib.sh.func.shared") in calls


def test_external_command_call_is_placeholder():
    nodes, edges = _resolve({
        "deploy.sh": "deploy() { git push; kubectl apply -f x; }",
    })
    git_id = ids.extern_function_key("git")
    ph = _node_by_id(nodes, git_id)
    assert ph is not None and ph.label == "Function"
    assert ph.properties["name"] == "git"
    assert ph.properties["resolved"] is False
    assert ph.properties["origin"] == "external"
    assert ph.properties["linkage_key"] == git_id
    calls = _edges(edges, "CALLS")
    assert calls[("deploy.sh.func.deploy", git_id)] == {"resolution": "external"}
    # kubectl too.
    assert _node_by_id(nodes, ids.extern_function_key("kubectl")) is not None


def test_builtins_produce_no_call_edge():
    nodes, edges = _resolve({
        "u.sh": "f() { echo hi; cd /tmp; local x=1; return 0; }",
    })
    assert all(e.rel_type != "CALLS" for e in edges)
    assert not any(n.key["id"].startswith("extern://bash/") for n in nodes)


def test_self_recursive_call_skipped():
    _, edges = _resolve({"r.sh": "fact() { fact; }"})
    assert all(e.rel_type != "CALLS" for e in edges)


def test_real_functions_carry_linkage_key():
    nodes, _ = _resolve({"lib.sh": "greet() { echo hi; }"})
    assert _by_name(nodes, "Function", "greet").properties["linkage_key"] == "extern://bash/greet"


def test_source_resolves_to_project_file():
    _, edges = _resolve({
        "lib/util.sh": "helper() { echo hi; }",
        "main.sh": "source ./lib/util.sh\nrun() { helper; }",
    })
    imports = _edges(edges, "IMPORTS")
    key = ("main.sh", "lib/util.sh")
    assert key in imports and imports[key] == {"resolution": "name"}


def test_source_external_becomes_placeholder():
    nodes, edges = _resolve({
        "main.sh": ". /etc/profile\n",
    })
    eid = ids.extern_file_key("/etc/profile")
    ph = _node_by_id(nodes, eid)
    assert ph is not None and ph.label == "File"
    assert ph.properties["resolved"] is False
    assert ph.properties["path"] == "/etc/profile"
    imports = _edges(edges, "IMPORTS")
    assert imports[("main.sh", eid)] == {"resolution": "external"}


def test_dynamic_source_is_skipped():
    nodes, edges = _resolve({
        "main.sh": 'source "$DIR/lib.sh"\n',
    })
    assert all(e.rel_type != "IMPORTS" for e in edges)
    assert not any(n.key["id"].startswith("extern://bash/file/") for n in nodes)


def test_no_type_or_implements_edges():
    _, edges = _resolve({
        "x.sh": "a() { b; }\nb() { echo hi; }",
    })
    for absent in ("IMPLEMENTS", "EXTENDS", "DEPENDS_ON", "EMBEDS"):
        assert all(e.rel_type != absent for e in edges)


def test_doc_property_reaches_nodes():
    nodes, _ = _resolve({
        "lib.sh": (
            "# greet prints a greeting.\n"
            "greet() { echo hi; }\n"
        ),
    })
    assert _by_name(nodes, "Function", "greet").properties["doc"] == "greet prints a greeting."
