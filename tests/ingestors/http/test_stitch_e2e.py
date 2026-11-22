"""End-to-end: the HTTP seam through the real ingest endpoint (not seeded nodes). A JS UI repo
and a C# backend repo are ingested -- separately, JS first -- into ONE graph; the http-endpoints
hook (run at the tail of each ingest) must leave the call unresolved after the JS repo and resolve
it once the C# repo lands. Proves the parse -> materialize -> stitch wiring and order-independence.
Tree-sitter only (deep='off'), so no .NET SDK is needed."""

from __future__ import annotations

import git
import pytest

from auryn.core import ingest
from auryn.core.graph_client import GraphWriter


def _commit(repo_dir, files, message):
    repo = git.Repo.init(str(repo_dir), initial_branch="main") if not (repo_dir / ".git").exists() \
        else git.Repo(str(repo_dir))
    with repo.config_writer() as cw:
        cw.set_value("user", "email", "t@e.com")
        cw.set_value("user", "name", "T")
    repo.index.add(files)
    return repo.index.commit(message).hexsha


def test_http_seam_wires_across_two_repos_order_independent(graph, tmp_path):
    w = GraphWriter(graph)

    # Repo A (UI): a standalone .js that calls /widgets/list. Ingested first.
    ui = tmp_path / "uiapp"
    ui.mkdir()
    (ui / "api.js").write_text('export function load() { $.get("/widgets/list"); }\n')
    ingest.ingest_directory(w, str(ui), deep="off", auto_install=False)

    # After the JS repo alone: the placeholder exists but is unresolved (no controller yet).
    assert graph.query(
        "MATCH (:Function {name:'load'})-[:CALLS_API]->(e:APIEndpoint {provenance:'http-derived'}) "
        "RETURN e.resolved"
    ).result_set == [[False]]

    # Repo B (backend): the C# controller, ingested later into the SAME graph.
    be = tmp_path / "backend"
    be.mkdir()
    (be / "Backend.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
                                       '<TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>\n')
    (be / "WidgetsController.cs").write_text(
        "namespace App {\n"
        "  public class WidgetsController {\n"
        "    public object List() { return null; }\n"
        "  }\n"
        "}\n"
    )
    ingest.ingest_directory(w, str(be), deep="off", auto_install=False)

    # The seam now completes end to end, across the two subgraphs, in one graph.
    assert graph.query(
        "MATCH (:Function {name:'load'})-[:CALLS_API]->(e:APIEndpoint {resolved:true})"
        "<-[:EXPOSES_ENDPOINT]-(:Method {name:'List'}) RETURN 1"
    ).result_set == [[1]]


def test_attribute_routed_declaration_and_call_merge_to_one_node(graph, tmp_path):
    """The dedup, through real ingest: an attribute-routed Web API endpoint and a caller on the same
    route+verb key to the SAME route-keyed node, so declaration and call CONVERGE -- one APIEndpoint,
    not a declared node plus a separate caller placeholder. Tree-sitter only (deep='off')."""
    w = GraphWriter(graph)

    ui = tmp_path / "ui2"
    ui.mkdir()
    (ui / "api.js").write_text('export function load() { $.get("/api/widgets"); }\n')
    ingest.ingest_directory(w, str(ui), deep="off", auto_install=False)

    be = tmp_path / "backend2"
    be.mkdir()
    (be / "Backend.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
                                       '<TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>\n')
    (be / "WidgetsController.cs").write_text(
        "using Microsoft.AspNetCore.Mvc;\n"
        "namespace App {\n"
        "  [ApiController]\n"
        "  [Route(\"api/[controller]\")]\n"
        "  public class WidgetsController : ControllerBase {\n"
        "    [HttpGet]\n"
        "    public object Get() { return null; }\n"
        "  }\n"
        "}\n"
    )
    ingest.ingest_directory(w, str(be), deep="off", auto_install=False)

    # Exactly ONE APIEndpoint for the route -- the declaration and the call merged (no duplicate).
    assert graph.query("MATCH (e:APIEndpoint {route:'/api/widgets'}) RETURN count(e)").result_set == [[1]]
    # ...and that single node carries BOTH sides of the golden path.
    assert graph.query(
        "MATCH (:Function {name:'load'})-[:CALLS_API]->(e:APIEndpoint {route:'/api/widgets', resolved:true})"
        "<-[:EXPOSES_ENDPOINT]-(:Method {name:'Get'}) RETURN count(e)"
    ).result_set == [[1]]


def test_endpoint_dedup_survives_incremental_resync(graph, tmp_path):
    """Delta-sync safety. The route-keyed endpoint node lives OUTSIDE the C# project's id-prefix, so a
    project resync can't reclaim it -- but the http-endpoints hook re-runs on every resync as a
    whole-graph reconcile and GCs it. Ingest a controller (git working tree -> incremental path),
    remove its endpoint across a commit, resync, and the endpoint is gone: no orphan, no duplicate."""
    w = GraphWriter(graph)
    repo_dir = tmp_path / "svc"
    repo_dir.mkdir()
    (repo_dir / "Svc.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
                                         '<TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>\n')
    controller = repo_dir / "WidgetsController.cs"
    controller.write_text(
        "using Microsoft.AspNetCore.Mvc;\n"
        "namespace App {\n"
        "  [ApiController]\n  [Route(\"api/[controller]\")]\n"
        "  public class WidgetsController : ControllerBase {\n"
        "    [HttpGet] public object Get() { return null; }\n"
        "  }\n}\n"
    )
    _commit(repo_dir, ["Svc.csproj", "WidgetsController.cs"], "v1")
    ingest.ingest_directory(w, str(repo_dir), deep="off", auto_install=False)

    # v1: the declared endpoint exists as exactly one node, marked resolved (it has a handler).
    assert graph.query(
        "MATCH (e:APIEndpoint {route:'/api/widgets', resolved:true}) RETURN count(e)"
    ).result_set == [[1]]

    # v2: drop the endpoint method entirely, commit, resync (incremental).
    controller.write_text(
        "namespace App {\n"
        "  public class WidgetsController {\n"
        "    public object Other() { return null; }\n"
        "  }\n}\n"
    )
    _commit(repo_dir, ["WidgetsController.cs"], "v2 -- endpoint removed")
    ingest.ingest_directory(w, str(repo_dir), deep="off", auto_install=False)

    # The C# resync deletes the Get method + its EXPOSES edge; the route-keyed node it orphans is
    # GC'd by the stitch -> zero, not a lingering orphan or a duplicate.
    assert graph.query("MATCH (:Method {name:'Get'}) RETURN count(*)").result_set == [[0]]
    assert graph.query("MATCH (e:APIEndpoint {route:'/api/widgets'}) RETURN count(e)").result_set == [[0]]


def test_endpoint_route_change_does_not_accumulate_duplicates(graph, tmp_path):
    """A resync that CHANGES an endpoint's route must not leave the old declared node behind as a
    duplicate. The old route becomes an unresolved node only if something still calls it; here nothing
    does, so it's GC'd, and the new route is the single declared node."""
    w = GraphWriter(graph)
    repo_dir = tmp_path / "svc2"
    repo_dir.mkdir()
    (repo_dir / "Svc.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
                                         '<TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>\n')
    controller = repo_dir / "WidgetsController.cs"

    def write(route):
        controller.write_text(
            "using Microsoft.AspNetCore.Mvc;\n"
            "namespace App {\n"
            f"  [ApiController]\n  [Route(\"{route}\")]\n"
            "  public class WidgetsController : ControllerBase {\n"
            "    [HttpGet] public object Get() { return null; }\n"
            "  }\n}\n"
        )

    write("api/widgets")
    _commit(repo_dir, ["Svc.csproj", "WidgetsController.cs"], "v1")
    ingest.ingest_directory(w, str(repo_dir), deep="off", auto_install=False)
    assert graph.query("MATCH (e:APIEndpoint) RETURN count(e)").result_set == [[1]]

    write("api/gadgets")
    _commit(repo_dir, ["WidgetsController.cs"], "v2 -- route moved")
    ingest.ingest_directory(w, str(repo_dir), deep="off", auto_install=False)

    # Exactly one endpoint, on the NEW route -- the old one didn't linger as a duplicate.
    assert graph.query("MATCH (e:APIEndpoint) RETURN e.route").result_set == [["/api/gadgets"]]


def test_endpoint_dedup_is_idempotent_across_noop_resyncs(graph, tmp_path):
    """Re-running ingest on UNCHANGED source must not drift: no duplicate endpoint, no accumulating
    caller/handler edges, stable `resolved`. The route-keyed node lives outside the project prefix, so
    this guards that repeated MERGEs plus the stitch's clear/rebuild/GC converge instead of piling up."""
    w = GraphWriter(graph)

    ui = tmp_path / "ui3"
    ui.mkdir()
    (ui / "api.js").write_text('export function load() { $.get("/api/widgets"); }\n')
    be = tmp_path / "be3"
    be.mkdir()
    (be / "Be.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
                                  '<TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>\n')
    (be / "WidgetsController.cs").write_text(
        "using Microsoft.AspNetCore.Mvc;\n"
        "namespace App {\n  [ApiController]\n  [Route(\"api/[controller]\")]\n"
        "  public class WidgetsController : ControllerBase {\n"
        "    [HttpGet] public object Get() { return null; }\n  }\n}\n"
    )

    def state():
        eps = graph.query("MATCH (e:APIEndpoint {route:'/api/widgets'}) RETURN count(e)").result_set[0][0]
        golden = graph.query(
            "MATCH (:Function {name:'load'})-[c:CALLS_API]->(e:APIEndpoint {route:'/api/widgets', resolved:true})"
            "<-[x:EXPOSES_ENDPOINT]-(:Method {name:'Get'}) RETURN count(c), count(x)"
        ).result_set[0]
        return eps, golden

    for _ in range(3):  # ingest both repos three times over -- nothing changes between rounds
        ingest.ingest_directory(w, str(ui), deep="off", auto_install=False)
        ingest.ingest_directory(w, str(be), deep="off", auto_install=False)
        assert state() == (1, [1, 1])  # one endpoint, one caller edge, one handler edge -- every round


def test_invokes_edge_lets_who_calls_cross_the_seam(graph, tmp_path):
    """The derived INVOKES edge collapses the CALLS_API/EXPOSES_ENDPOINT "V" (caller and handler both
    point INTO the endpoint node) into one call-like edge, so a single-direction reachability walk --
    who_calls upstream, impact_of -- crosses the HTTP boundary instead of dying at it. Before this
    edge, who_calls(handler) stopped at the first repo; now the cross-repo caller shows up in one
    traversal."""
    from auryn.core import graph_queries

    w = GraphWriter(graph)

    ui = tmp_path / "uiapp"
    ui.mkdir()
    (ui / "api.js").write_text('export function load() { $.get("/widgets/list"); }\n')
    ingest.ingest_directory(w, str(ui), deep="off", auto_install=False)

    be = tmp_path / "backend"
    be.mkdir()
    (be / "Backend.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
                                       '<TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>\n')
    (be / "WidgetsController.cs").write_text(
        "namespace App {\n"
        "  public class WidgetsController {\n"
        "    public object List() { return null; }\n"
        "  }\n"
        "}\n"
    )
    ingest.ingest_directory(w, str(be), deep="off", auto_install=False)

    # A direct, HTTP-tagged INVOKES edge now connects the JS caller to the C# handler, across repos.
    assert graph.query(
        "MATCH (:Function {name:'load'})-[r:INVOKES {via:'http'}]->(:Method {name:'List'}) RETURN count(r)"
    ).result_set == [[1]]

    # ...and who_calls(List) -- which follows CALLS|INVOKES -- returns the cross-repo caller in a
    # single traversal (it would have returned nothing across the seam before this edge existed).
    handler_id = graph.query("MATCH (m:Method {name:'List'}) RETURN m.id").result_set[0][0]
    result = graph_queries.callers(graph, handler_id, max_depth=3)
    assert "load" in {n["name"] for n in result["nodes"]}

    # The seam edge is idempotent: re-running the stitch (via a no-op re-ingest) keeps exactly one.
    ingest.ingest_directory(w, str(be), deep="off", auto_install=False)
    assert graph.query(
        "MATCH (:Function {name:'load'})-[r:INVOKES]->(:Method {name:'List'}) RETURN count(r)"
    ).result_set == [[1]]



_HTTP_CALLERS = {
    # caller file, `load` with two HTTP calls, `load` with neither
    "typescript": (
        "api.js",
        'export function load() { $.get("/api/widgets/5"); fetch("/api/missing"); }\n',
        "export function load() { return 1; }\n",
    ),
    "csharp": (
        "Gateway.cs",
        "namespace Ui {\n  public class Gateway {\n    public object load() {\n"
        '      _client.GetAsync("/api/widgets/5");\n      _client.GetAsync("/api/missing");\n'
        "      return null;\n    }\n  }\n}\n",
        "namespace Ui {\n  public class Gateway {\n    public object load() { return null; }\n  }\n}\n",
    ),
}


@pytest.mark.parametrize(
    ("caller", "git_tracked"),
    [("typescript", False), ("typescript", True), ("csharp", False)],
    ids=["ts-full-recompute", "ts-delta", "csharp-full-recompute"],
)
def test_removed_http_call_drops_its_seam(graph, tmp_path, monkeypatch, caller, git_tracked):
    """Removing an outbound HTTP call removes its CALLS_API edge, the INVOKES edge the stitch built
    from it, and the caller-side endpoint it leaves without a referrer -- on a full recompute (a plain
    directory; C# always recomputes) and on a delta resync (a git working tree) alike. The declared
    endpoint stays, since its controller still exposes it. Regression: the full-sync edge diff skipped
    every `extern://http/` target and no delta spec owned CALLS_API, so a deleted call stayed in the
    graph and still traced to its handler."""
    from auryn.core import incremental_scoped

    delta_runs = []
    real_incremental_sync = incremental_scoped.incremental_sync

    def spy(*args, **kwargs):
        delta_runs.append(kwargs.get("project_name"))
        return real_incremental_sync(*args, **kwargs)

    monkeypatch.setattr(incremental_scoped, "incremental_sync", spy)
    w = GraphWriter(graph)

    be = tmp_path / "backend"
    be.mkdir()
    (be / "Backend.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
                                       '<TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>\n')
    (be / "WidgetsController.cs").write_text(
        "using Microsoft.AspNetCore.Mvc;\n"
        "namespace App {\n  [ApiController]\n  [Route(\"api/[controller]\")]\n"
        "  public class WidgetsController : ControllerBase {\n"
        "    [HttpGet(\"{id}\")] public object Get(int id) { return null; }\n  }\n}\n"
    )
    ingest.ingest_directory(w, str(be), deep="off", auto_install=False)

    # One call the stitch resolves by route template (/api/widgets/5 ~ api/widgets/{id}), one it can't.
    file_name, with_calls, without_calls = _HTTP_CALLERS[caller]
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / file_name).write_text(with_calls)
    if git_tracked:
        _commit(ui, [file_name], "v1")
    ingest.ingest_directory(w, str(ui), deep="off", auto_install=False)

    assert graph.query("MATCH ({name:'load'})-[r:CALLS_API]->() RETURN count(r)").result_set == [[2]]
    assert graph.query(
        "MATCH ({name:'load'})-[r:INVOKES]->(:Method {name:'Get'}) RETURN count(r)"
    ).result_set == [[1]]

    (ui / file_name).write_text(without_calls)
    if git_tracked:
        _commit(ui, [file_name], "v2 -- calls removed")
    ingest.ingest_directory(w, str(ui), deep="off", auto_install=False)

    assert bool(delta_runs) == git_tracked  # the path under test is the one that ran
    assert graph.query("MATCH ()-[r:CALLS_API]->() RETURN count(r)").result_set == [[0]]
    assert graph.query("MATCH ()-[r:INVOKES]->() RETURN count(r)").result_set == [[0]]
    assert graph.query(
        "MATCH (e:APIEndpoint)<-[:EXPOSES_ENDPOINT]-(:Method {name:'Get'}) RETURN e.route"
    ).result_set == [["/api/widgets/{id}"]]
    assert graph.query("MATCH (e:APIEndpoint) RETURN count(e)").result_set == [[1]]
