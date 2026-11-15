"""Integration tests for the Roslyn C# engine, against the real `graph` fixture and
the buildable DI fixture solution in tests/fixtures/csharp_di.

Gated on the .NET SDK: skipped entirely when `dotnet` isn't on PATH (mirroring how
the PL/SQL tests gate on an Oracle container). To avoid a `dotnet run` build on every
extraction, the tool is published once per session and pointed at via the
AURYN_ROSLYN_EXTRACTOR override.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import git
import pytest

from auryn.core.graph_client import GraphWriter
from auryn.ingestors.csharp.git_sync import GitCSharpProjectConfig, sync

pytestmark = pytest.mark.skipif(shutil.which("dotnet") is None, reason="requires the .NET SDK")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_FIXTURE = _REPO_ROOT / "tests" / "fixtures" / "csharp_di"
_SOURCES = ["Sample.csproj", "Framework.cs", "Greeting.cs"]


@pytest.fixture(scope="session")
def roslyn_tool_env(tmp_path_factory):
    """Publish the extractor once and expose it via AURYN_ROSLYN_EXTRACTOR so each
    extraction runs the prebuilt binary instead of `dotnet run` (which rebuilds)."""
    out = tmp_path_factory.mktemp("roslyn-extractor")
    project = _REPO_ROOT / "tools" / "roslyn-extractor" / "RoslynExtractor.csproj"
    result = subprocess.run(
        ["dotnet", "publish", str(project), "-c", "Release", "-o", str(out), "--nologo", "-v", "quiet"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"could not publish the Roslyn extractor: {result.stderr or result.stdout}")
    dll = out / "roslyn-extractor.dll"
    previous = os.environ.get("AURYN_ROSLYN_EXTRACTOR")
    os.environ["AURYN_ROSLYN_EXTRACTOR"] = str(dll)
    yield
    if previous is None:
        os.environ.pop("AURYN_ROSLYN_EXTRACTOR", None)
    else:
        os.environ["AURYN_ROSLYN_EXTRACTOR"] = previous


@pytest.fixture
def fixture_repo(tmp_path):
    origin = tmp_path / "SampleRepo"
    origin.mkdir()
    for name in _SOURCES:
        shutil.copy(_FIXTURE / name, origin / name)
    repo = git.Repo.init(str(origin), initial_branch="main")
    with repo.config_writer() as cw:
        cw.set_value("user", "email", "t@e.com")
        cw.set_value("user", "name", "T")
    repo.index.add(_SOURCES)
    commit = repo.index.commit("init").hexsha
    return origin, commit


def _sync(graph, origin, commit, tmp_path, engine="roslyn"):
    config = GitCSharpProjectConfig(repo_uri=str(origin), project_name="Sample")
    return sync(GraphWriter(graph), config, ref=commit, cache_root=tmp_path / f"cache-{engine}", engine=engine)


def test_roslyn_resolves_di_registration(roslyn_tool_env, graph, fixture_repo, tmp_path):
    origin, commit = fixture_repo
    _sync(graph, origin, commit, tmp_path)

    assert graph.query(
        "MATCH (d:DIRegistration) RETURN d.service_type, d.implementation_type, d.lifetime"
    ).result_set == [["IGreeter", "Greeter", "Scoped"]]
    assert graph.query(
        "MATCH (d:DIRegistration)-[r:REGISTERS]->(t) RETURN r.role, labels(t)[0], t.name ORDER BY r.role"
    ).result_set == [["implementation", "Class", "Greeter"], ["service", "Interface", "IGreeter"]]
    assert graph.query(
        "MATCH (:Interface {name:'IGreeter'})-[:RESOLVES_TO]->(c:Class {name:'Greeter'}) RETURN c.name"
    ).result_set == [["Greeter"]]


def test_roslyn_interface_call_resolves_to_implementation(roslyn_tool_env, graph, fixture_repo, tmp_path):
    origin, commit = fixture_repo
    _sync(graph, origin, commit, tmp_path)

    # The static call to the interface method is recorded...
    assert graph.query(
        "MATCH (:Method {name:'Run'})-[:CALLS]->(m:Method {name:'Greet'})<-[:HAS_METHOD]-(:Interface {name:'IGreeter'}) RETURN 1"
    ).result_set == [[1]]
    # ...and, crucially, a di-resolved call reaches the concrete implementation. The call's
    # provenance rides on the shared `resolution` key (normalized from the Roslyn tool's
    # `provenance` in _to_graph_items, so both C# engines agree with Python/C).
    assert graph.query(
        "MATCH (:Method {name:'Run'})-[r:CALLS {resolution:'di-resolved'}]->(m:Method {name:'Greet'})"
        "<-[:HAS_METHOD]-(:Class {name:'Greeter'}) RETURN r.resolution"
    ).result_set == [["di-resolved"]]


def test_roslyn_extends_implements_without_naming_heuristics(roslyn_tool_env, graph, fixture_repo, tmp_path):
    origin, commit = fixture_repo
    _sync(graph, origin, commit, tmp_path)

    assert graph.query("MATCH (:Class {name:'Greeter'})-[:IMPLEMENTS]->(:Interface {name:'IGreeter'}) RETURN 1").result_set == [[1]]
    assert graph.query("MATCH (:Class {name:'GreetController'})-[:EXTENDS]->(:Class {name:'ControllerBase'}) RETURN 1").result_set == [[1]]


def test_roslyn_endpoints_and_dtos(roslyn_tool_env, graph, fixture_repo, tmp_path):
    origin, commit = fixture_repo
    _sync(graph, origin, commit, tmp_path)

    # Route normalized (lowercased + leading slash) and the node route-keyed, so a caller reaching
    # `/api/greet` merges onto this same declaration instead of duplicating it (ADR 0006).
    assert graph.query("MATCH (e:APIEndpoint) RETURN e.route, e.http_method").result_set == [["/api/greet", "GET"]]
    assert graph.query("MATCH (e:APIEndpoint) RETURN e.id").result_set == [["extern://http/GET:/api/greet"]]
    assert graph.query(
        "MATCH (e:APIEndpoint)-[r]->(d:DTO) RETURN type(r), d.name ORDER BY d.name"
    ).result_set == [["ACCEPTS", "GreetingRequest"], ["RETURNS", "GreetingResponse"]]


def test_roslyn_calls_carry_argument_and_type_metadata(roslyn_tool_env, graph, fixture_repo, tmp_path):
    origin, commit = fixture_repo
    _sync(graph, origin, commit, tmp_path)

    # Generic type arguments captured on a generic call: services.AddScoped<IGreeter, Greeter>()
    assert graph.query(
        "MATCH (:Method {name:'ConfigureServices'})-[c:CALLS]->(:Method {name:'AddScoped'}) RETURN c.type_arguments"
    ).result_set == [[["Greeter", "IGreeter"]]]
    # Argument value types captured on an ordinary call: _greeter.Greet(name) where name is a string.
    assert graph.query(
        "MATCH (:Method {name:'Run'})-[c:CALLS]->(:Method {name:'Greet'}) RETURN DISTINCT c.argument_types"
    ).result_set == [[["string"]]]


def test_roslyn_nodes_carry_line_range_provenance(roslyn_tool_env, graph, fixture_repo, tmp_path):
    origin, commit = fixture_repo
    _sync(graph, origin, commit, tmp_path)

    rows = graph.query(
        "MATCH (c:Class {name:'Greeter'}) RETURN c.file, c.line_start, c.line_end"
    ).result_set
    assert rows[0][0] == "Greeting.cs"
    assert rows[0][1] >= 1 and rows[0][2] >= rows[0][1]


def test_roslyn_resync_same_commit_is_a_noop(roslyn_tool_env, graph, fixture_repo, tmp_path):
    origin, commit = fixture_repo
    _sync(graph, origin, commit, tmp_path)
    again = _sync(graph, origin, commit, tmp_path)
    assert (again.nodes_added, again.nodes_removed, again.edges_added, again.edges_removed) == (0, 0, 0, 0)


_EXTERNAL_CALLS_CS = """
using System;

namespace Acme
{
    public class Reporter
    {
        public void Emit(string line)
        {
            Console.WriteLine(line);
        }
    }
}
"""


def test_roslyn_unresolved_bcl_call_becomes_a_placeholder(roslyn_tool_env, graph, tmp_path):
    """ADR 0004: a call the semantic model binds outside this ingest (here the BCL
    Console.WriteLine) is no longer dropped -- it surfaces as a first-class placeholder
    Method reached by an ordinary CALLS edge, and real externally-visible methods carry a
    cross-repo linkage key so a later ingest of the defining repo can heal the reference."""
    origin = tmp_path / "ExternRepo"
    origin.mkdir()
    (origin / "Reporter.cs").write_text(_EXTERNAL_CALLS_CS)
    repo = git.Repo.init(str(origin), initial_branch="main")
    with repo.config_writer() as cw:
        cw.set_value("user", "email", "t@e.com")
        cw.set_value("user", "name", "T")
    repo.index.add(["Reporter.cs"])
    commit = repo.index.commit("init").hexsha

    config = GitCSharpProjectConfig(repo_uri=str(origin), project_name="Extern")
    sync(GraphWriter(graph), config, ref=commit, cache_root=tmp_path / "cache", engine="roslyn")

    # The BCL call surfaced as a placeholder, indistinguishable in shape from a resolved callee.
    assert graph.query(
        "MATCH (:Method {name:'Emit'})-[c:CALLS {resolution:'external'}]->(m:Method {resolved:false}) "
        "RETURN m.id, m.origin, m.qualified_name, m.linkage_key"
    ).result_set == [[
        "extern://csharp/System.Console.WriteLine",
        "stdlib",
        "System.Console.WriteLine",
        "extern://csharp/System.Console.WriteLine",
    ]]

    # The real, externally-visible method carries its project-independent linkage key.
    assert graph.query(
        "MATCH (m:Method {name:'Emit'}) RETURN m.linkage_key"
    ).result_set == [["extern://csharp/Acme.Reporter.Emit"]]


def test_roslyn_type_ids_match_the_treesitter_engine(roslyn_tool_env, graph, falkordb_connection, fixture_repo, tmp_path):
    """The two engines must be interchangeable in one graph: for the structural
    backbone (namespaces, files, type declarations) their node ids are identical, so
    switching engines never orphans nodes."""
    from uuid import uuid4

    origin, commit = fixture_repo
    _sync(graph, origin, commit, tmp_path, engine="roslyn")

    treesitter_graph = falkordb_connection.select_graph(f"ts_{uuid4().hex}")
    try:
        _sync(treesitter_graph, origin, commit, tmp_path, engine="treesitter")
        backbone = "MATCH (n) WHERE n:Namespace OR n:File OR n:Class OR n:Interface OR n:DTO OR n:Struct OR n:Enum OR n:Record OR n:Delegate RETURN n.id ORDER BY n.id"
        roslyn_ids = [r[0] for r in graph.query(backbone).result_set]
        treesitter_ids = [r[0] for r in treesitter_graph.query(backbone).result_set]
        assert roslyn_ids == treesitter_ids
    finally:
        treesitter_graph.delete()


_BFF_CSPROJ = ('<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
               '<TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>\n')
# Self-contained: a local HttpClient stub (Name == "HttpClient", so the extractor's type-based
# detection fires) -- no dependency on the real System.Net.Http being referenced.
_BFF_GATEWAY = """
namespace Fake {
  public class HttpClient {
    public System.Threading.Tasks.Task<string> GetAsync(string url) => null!;
    public System.Threading.Tasks.Task<string> PostAsJsonAsync(string url, object body) => null!;
  }
}
namespace Bff {
  public class Gateway {
    private readonly Fake.HttpClient _client = new();
    public async System.Threading.Tasks.Task Load(object w) {
      await _client.GetAsync("/api/widgets");
      await _client.PostAsJsonAsync("/api/widgets", w);
    }
  }
}
"""


def _bff_repo(tmp_path):
    origin = tmp_path / "BffRepo"
    origin.mkdir()
    (origin / "Bff.csproj").write_text(_BFF_CSPROJ)
    (origin / "Gateway.cs").write_text(_BFF_GATEWAY)
    repo = git.Repo.init(str(origin), initial_branch="main")
    with repo.config_writer() as cw:
        cw.set_value("user", "email", "t@e.com")
        cw.set_value("user", "name", "T")
    repo.index.add(["Bff.csproj", "Gateway.cs"])
    return origin, repo.index.commit("init").hexsha


def test_roslyn_httpclient_call_becomes_route_seam(roslyn_tool_env, graph, tmp_path):
    # The deep (Roslyn) pass detects HttpClient calls by type and emits the same route-keyed
    # CALLS_API -> APIEndpoint placeholder the tree-sitter pass does (ADR 0006).
    origin, commit = _bff_repo(tmp_path)
    config = GitCSharpProjectConfig(repo_uri=str(origin), project_name="Bff")
    sync(GraphWriter(graph), config, ref=commit, cache_root=tmp_path / "cache", engine="roslyn")

    assert graph.query(
        "MATCH (:Method {name:'Load'})-[r:CALLS_API {provenance:'http-derived'}]->"
        "(e:APIEndpoint {route:'/api/widgets', resolved:false}) "
        "RETURN e.http_method ORDER BY e.http_method"
    ).result_set == [["GET"], ["POST"]]


def test_deep_inline_two_phase_keeps_the_http_seam(roslyn_tool_env, graph, tmp_path):
    # The clobber check: with deep='inline' the fast (tree-sitter) pass emits the seam, then the
    # Roslyn deep pass runs -- and because BOTH emit the identical CALLS_API/APIEndpoint ids, the
    # seam SURVIVES (it isn't deleted by the deep delta). This is what made C# two-engine hard.
    from auryn.core import ingest

    origin, _ = _bff_repo(tmp_path)
    ingest.ingest_directory(GraphWriter(graph), str(origin), deep="inline", auto_install=False)

    assert graph.query(
        "MATCH (:Method {name:'Load'})-[:CALLS_API {provenance:'http-derived'}]->"
        "(e:APIEndpoint {provenance:'http-derived'}) RETURN count(e)"
    ).result_set == [[2]]


def test_roslyn_captures_interpolated_url(roslyn_tool_env, graph, tmp_path):
    # The deep pass renders an interpolated URL as a route template too ($"...{id}" -> {param}).
    origin = tmp_path / "BffI"
    origin.mkdir()
    (origin / "Bff.csproj").write_text(_BFF_CSPROJ)
    (origin / "G.cs").write_text(
        "namespace Fake { public class HttpClient {\n"
        "  public System.Threading.Tasks.Task<string> GetAsync(string u) => null!; } }\n"
        "namespace Bff { public class G {\n"
        "  private readonly Fake.HttpClient _client = new();\n"
        "  public async System.Threading.Tasks.Task ById(int id) { await _client.GetAsync($\"/api/widgets/{id}\"); }\n"
        "}}\n"
    )
    repo = git.Repo.init(str(origin), initial_branch="main")
    with repo.config_writer() as cw:
        cw.set_value("user", "email", "t@e.com")
        cw.set_value("user", "name", "T")
    repo.index.add(["Bff.csproj", "G.cs"])
    commit = repo.index.commit("init").hexsha
    config = GitCSharpProjectConfig(repo_uri=str(origin), project_name="BffI")
    sync(GraphWriter(graph), config, ref=commit, cache_root=tmp_path / "c", engine="roslyn")

    assert graph.query(
        "MATCH (e:APIEndpoint {provenance:'http-derived'}) RETURN e.route"
    ).result_set == [["/api/widgets/{param}"]]
