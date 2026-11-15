"""Tests for the Kendo/ASP.NET MVC view-tier ingestion.

Two layers: pure-Python parser tests over hand-written `.cshtml` snippets (no DB, no
.NET), and an integration test that git-syncs the csharp_mvc fixture with the
tree-sitter engine (so it needs neither Oracle nor the .NET SDK) and asserts the UI
seam edges land on the real controller actions.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import git

from auryn.core.graph_client import GraphWriter
from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.ingestors.csharp import kendo
from auryn.ingestors.csharp.git_sync import GitCSharpProjectConfig, sync

_REPO_ROOT = Path(__file__).resolve().parents[3]
_FIXTURE = _REPO_ROOT / "tests" / "fixtures" / "csharp_mvc"

# A representative action index, as the C# engine would produce it.
_PROJ = "csharp://Widgets"
_CTRL = f"{_PROJ}/ns/sample.web.class.widgetscontroller.method"
_ACTIONS = {
    ("widgets", "index"): f"{_CTRL}.index()",
    ("widgets", "list"): f"{_CTRL}.list()",
    ("widgets", "save"): f"{_CTRL}.save(sample.web.widgetdto)",
}


def _nodes(items):
    return [i for i in items if isinstance(i, GraphNode)]


def _edges(items):
    return [i for i in items if isinstance(i, GraphEdge)]


def _edge(items, rel_type):
    return [e for e in _edges(items) if e.rel_type == rel_type]


# -- Parser (pure Python) -----------------------------------------------------------


def test_parser_extracts_htmlhelper_grid_and_seam_edges():
    source = """
    @(Html.Kendo().Grid<WidgetDto>()
        .Name("widgetsGrid")
        .Columns(c => { c.Bound(w => w.Id); c.Bound(w => w.Name); })
        .DataSource(d => d.Ajax()
            .Read(r => r.Action("List", "Widgets"))
            .Update(u => u.Action("Save", "Widgets")))
        .Events(e => e.Change("onWidgetChange")))
    """
    items = kendo._extract_view("Views/Widgets/Index.cshtml", source, _PROJ, _ACTIONS)

    assert {n.label for n in _nodes(items)} == {"View", "Grid", "Field", "EventHandler"}
    assert {n.properties["name"] for n in _nodes(items) if n.label == "Field"} == {"Id", "Name"}

    displays = _edge(items, "DISPLAYS_DATA")
    assert [e.to_key["id"] for e in displays] == [_ACTIONS[("widgets", "list")]]
    calls = _edge(items, "CALLS_API")
    assert [(e.to_key["id"], e.properties.get("operation")) for e in calls] == [
        (_ACTIONS[("widgets", "save")], "update")
    ]
    handles = _edge(items, "HANDLES")
    assert handles and handles[0].properties["event"] == "Change"


def test_parser_extracts_client_js_grid():
    source = """
    <script>
    $("#widgetsGrid").kendoGrid({
        columns: [{ field: "Id" }, { field: "Name" }],
        dataSource: { transport: {
            read:   { url: "/Widgets/List" },
            update: { url: "/Widgets/Save" }
        }},
        change: onWidgetChange
    });
    </script>
    """
    items = kendo._extract_view("Views/Widgets/Index.cshtml", source, _PROJ, _ACTIONS)

    assert {n.properties["name"] for n in _nodes(items) if n.label == "Field"} == {"Id", "Name"}
    assert [e.to_key["id"] for e in _edge(items, "DISPLAYS_DATA")] == [_ACTIONS[("widgets", "list")]]
    assert [e.to_key["id"] for e in _edge(items, "CALLS_API")] == [_ACTIONS[("widgets", "save")]]


def test_parser_extracts_form_submits_to():
    source = '@using (Html.BeginForm("Save", "Widgets")) { <input type="submit" /> }'
    items = kendo._extract_view("Views/Widgets/Edit.cshtml", source, _PROJ, _ACTIONS)
    assert [e.to_key["id"] for e in _edge(items, "SUBMITS_TO")] == [_ACTIONS[("widgets", "save")]]


def test_parser_drops_unresolved_action_but_keeps_grid():
    source = """
    @(Html.Kendo().Grid<WidgetDto>().Name("g")
        .DataSource(d => d.Read(r => r.Action("Missing", "Nowhere"))))
    """
    items = kendo._extract_view("Views/Widgets/Index.cshtml", source, _PROJ, _ACTIONS)
    assert any(n.label == "Grid" for n in _nodes(items))  # node kept
    assert _edge(items, "DISPLAYS_DATA") == [] and _edge(items, "CALLS_API") == []  # edge dropped


def test_parser_resolves_url_action_in_js_transport():
    # @Url.Action(...) in a client-JS transport url -- the dominant idiom whose nested quotes
    # previously defeated the plain url regex, so the seam edge was dropped.
    source = '''
    <script>
    $("#widgetsGrid").kendoGrid({
        dataSource: { transport: {
            read:   { url: "@Url.Action("List", "Widgets")" },
            update: { url: "@Url.Action("Save", "Widgets")" }
        }}
    });
    </script>
    '''
    items = kendo._extract_view("Views/Widgets/Index.cshtml", source, _PROJ, _ACTIONS)
    assert [e.to_key["id"] for e in _edge(items, "DISPLAYS_DATA")] == [_ACTIONS[("widgets", "list")]]
    assert [e.to_key["id"] for e in _edge(items, "CALLS_API")] == [_ACTIONS[("widgets", "save")]]


def test_parser_url_action_without_controller_uses_default():
    # @Url.Action("List") omits the controller -> falls back to the view's MVC-convention controller.
    source = '''
    <script>
    $("#g").kendoGrid({ dataSource: { transport: {
        read: { url: "@Url.Action("List")" }
    }}});
    </script>
    '''
    items = kendo._extract_view("Views/Widgets/Index.cshtml", source, _PROJ, _ACTIONS)
    assert [e.to_key["id"] for e in _edge(items, "DISPLAYS_DATA")] == [_ACTIONS[("widgets", "list")]]


def test_controller_action_from_url_helper():
    assert kendo._controller_action_from_url("/Widgets/List") == ("Widgets", "List")
    assert kendo._controller_action_from_url('@Url.Action("List", "Widgets")') == ("Widgets", "List")
    assert kendo._controller_action_from_url('@Url.Action("List")') == (None, "List")
    assert kendo._controller_action_from_url("https://x/y/z") is None


def test_action_index_honours_actionname_override():
    # A method GetAll decorated [ActionName("List")] is routable as BOTH "getall" and "list".
    ctrl = f"{_PROJ}/ns/sample.web.class.widgetscontroller"
    method_id = f"{ctrl}.method.getall()"
    items = [
        GraphNode("Class", {"id": ctrl}, {"name": "WidgetsController"}),
        GraphNode("Method", {"id": method_id}, {"name": "GetAll", "action_name": "List"}),
    ]
    index = kendo._build_action_index(items)
    assert index[("widgets", "getall")] == method_id
    assert index[("widgets", "list")] == method_id  # reachable via the [ActionName] override


def test_parser_extracts_actionlink_and_child_action_as_calls_api():
    source = """
    @Html.ActionLink("All widgets", "List", "Widgets")
    <div>@Html.Action("Index", "Widgets")</div>
    @{ Html.RenderAction("Save", "Widgets"); }
    """
    items = kendo._extract_view("Views/Home/Dashboard.cshtml", source, _PROJ, _ACTIONS)
    targets = {e.to_key["id"] for e in _edge(items, "CALLS_API")}
    assert targets == {
        _ACTIONS[("widgets", "list")],
        _ACTIONS[("widgets", "index")],
        _ACTIONS[("widgets", "save")],
    }
    assert all(e.from_label == "View" for e in _edge(items, "CALLS_API"))


def test_action_name_helper_reads_attribute():
    from auryn.ingestors.csharp import resolution
    from auryn.ingestors.csharp.raw_model import RawAttribute

    assert resolution._action_name([RawAttribute("ActionName", ["List"])]) == "List"
    assert resolution._action_name([RawAttribute("ActionNameAttribute", ['"List"'])]) == "List"
    assert resolution._action_name([RawAttribute("Route", ["api/x"])]) is None
    assert resolution._action_name([]) is None


# -- Integration (tree-sitter engine, no .NET SDK needed) ---------------------------


def test_git_sync_wires_the_view_into_the_c_sharp_controller(graph, tmp_path):
    origin = tmp_path / "WidgetsRepo"
    origin.mkdir()
    files = ["Sample.csproj", "Mvc.cs", "WidgetsController.cs", "Views/Widgets/Index.cshtml"]
    for rel in files:
        dest = origin / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(_FIXTURE / rel, dest)
    repo = git.Repo.init(str(origin), initial_branch="main")
    with repo.config_writer() as cw:
        cw.set_value("user", "email", "t@e.com")
        cw.set_value("user", "name", "T")
    repo.index.add(files)
    commit = repo.index.commit("init").hexsha

    config = GitCSharpProjectConfig(repo_uri=str(origin), project_name="Widgets")
    sync(GraphWriter(graph), config, ref=commit, cache_root=tmp_path / "cache", engine="treesitter")

    def q(cypher):
        return graph.query(cypher).result_set

    # The view tier exists and is contained correctly.
    assert q("MATCH (v:View)-[:CONTAINS]->(g:Grid {name:'widgetsGrid'}) RETURN v.name") == [["Index.cshtml"]]
    assert {r[0] for r in q("MATCH (:Grid)-[:CONTAINS]->(f:Field) RETURN f.name")} == {"Id", "Name"}
    assert q("MATCH (h:EventHandler)-[:HANDLES]->(:Grid) RETURN h.name, h.event") == [["onWidgetChange", "Change"]]

    # The seam: grid/form edges resolve to the real WidgetsController actions.
    assert q(
        "MATCH (:Grid)-[:DISPLAYS_DATA]->(m:Method {name:'List'})<-[:HAS_METHOD]-(:Class {name:'WidgetsController'}) RETURN 1"
    ) == [[1]]
    assert q(
        "MATCH (:Grid)-[r:CALLS_API]->(m:Method {name:'Save'})<-[:HAS_METHOD]-(:Class {name:'WidgetsController'}) RETURN r.operation"
    ) == [["update"]]
    assert q(
        "MATCH (:View)-[:SUBMITS_TO]->(m:Method {name:'Save'})<-[:HAS_METHOD]-(:Class {name:'WidgetsController'}) RETURN 1"
    ) == [[1]]


def test_git_sync_resync_same_commit_is_a_noop(graph, tmp_path):
    origin = tmp_path / "WidgetsRepo"
    origin.mkdir()
    files = ["Sample.csproj", "Mvc.cs", "WidgetsController.cs", "Views/Widgets/Index.cshtml"]
    for rel in files:
        dest = origin / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(_FIXTURE / rel, dest)
    repo = git.Repo.init(str(origin), initial_branch="main")
    with repo.config_writer() as cw:
        cw.set_value("user", "email", "t@e.com")
        cw.set_value("user", "name", "T")
    repo.index.add(files)
    commit = repo.index.commit("init").hexsha

    config = GitCSharpProjectConfig(repo_uri=str(origin), project_name="Widgets")
    cache = tmp_path / "cache"
    sync(GraphWriter(graph), config, ref=commit, cache_root=cache, engine="treesitter")
    again = sync(GraphWriter(graph), config, ref=commit, cache_root=cache, engine="treesitter")
    assert (again.nodes_added, again.nodes_removed, again.edges_added, again.edges_removed) == (0, 0, 0, 0)
