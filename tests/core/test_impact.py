"""Diff/PR-aware impact: the unified-diff parser (pure) + impact_of_diff over a seeded
two-service graph, plus a real git round-trip. Embedded-FalkorDB tests skip without the extra."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from auryn.cli import cli
from auryn.core.impact import git_diff, git_toplevel, impact_of_diff, parse_unified_diff, symbols_in_ranges


# -- parse_unified_diff (pure) ---------------------------------------------------------------


def test_parse_modified_file_hunk():
    diff = ("diff --git a/orders/db.py b/orders/db.py\n--- a/orders/db.py\n+++ b/orders/db.py\n"
            "@@ -10,3 +10,2 @@\n-a\n-b\n-c\n+x\n+y\n")
    changed, deleted = parse_unified_diff(diff)
    assert changed == {"orders/db.py": [(10, 11)]}  # +10,2 -> lines 10..11
    assert deleted == []


def test_parse_count_omitted_is_single_line():
    changed, _ = parse_unified_diff("--- a/x.py\n+++ b/x.py\n@@ -5 +7 @@\n-a\n+b\n")
    assert changed == {"x.py": [(7, 7)]}


def test_parse_new_file():
    diff = "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1,4 @@\n+a\n+b\n+c\n+d\n"
    changed, deleted = parse_unified_diff(diff)
    assert changed == {"new.py": [(1, 4)]} and deleted == []


def test_parse_deleted_file_is_full_span():
    diff = "diff --git a/gone.py b/gone.py\ndeleted file mode 100644\n--- a/gone.py\n+++ /dev/null\n@@ -1,5 +0,0 @@\n-x\n"
    changed, deleted = parse_unified_diff(diff)
    assert deleted == ["gone.py"]
    assert changed["gone.py"] == [(0, 10**9)]  # whole file -> every symbol counts as changed


def test_parse_multiple_hunks_and_files():
    diff = (
        "--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-x\n+y\n@@ -20,0 +21,3 @@\n+n\n+o\n+p\n"
        "--- a/b.py\n+++ b/b.py\n@@ -5,2 +5,0 @@\n-z\n"
    )
    changed, _ = parse_unified_diff(diff)
    assert changed["a.py"] == [(1, 1), (21, 23)]
    assert changed["b.py"] == [(5, 5)]  # +5,0 deletion -> anchored at 5


def test_parse_counts_the_changed_lines_not_the_context():
    """Regression: the ranges came from the hunk header, which spans the context lines too, so a
    plain `git diff` (three lines of context) of a one-line change to `refund` also reported the
    function above it."""
    diff = (
        "--- a/charges.py\n+++ b/charges.py\n@@ -16,6 +16,6 @@ def charge(customer_id):\n"
        "     return record(customer_id)\n \n \n-def refund(payment_id):\n+def refund(payment_id, reason=None):\n"
        "     payment = submit_refund(payment_id)\n     return record_refund(payment)\n"
    )
    assert parse_unified_diff(diff)[0] == {"charges.py": [(19, 19)]}


def test_parse_a_removal_is_the_line_above_it():
    """Removed lines alone are anchored at the new line above them -- git's own anchor for a
    `-U0` deletion -- with or without context around them."""
    with_context = "--- a/m.py\n+++ b/m.py\n@@ -4,7 +4,5 @@\n a\n b\n c\n-x\n-y\n d\n e\n"
    without = "--- a/m.py\n+++ b/m.py\n@@ -7,2 +6,0 @@\n-x\n-y\n"
    assert parse_unified_diff(with_context)[0] == parse_unified_diff(without)[0] == {"m.py": [(6, 6)]}


def test_parse_a_removed_sql_comment_is_a_body_line():
    """A removed `-- note` shows as `--- note`: the hunk's line counts, not the prefix, say it is
    a removed line and not the next file's header."""
    diff = "--- a/q.sql\n+++ b/q.sql\n@@ -3,2 +3,1 @@\n--- note\n select 1;\n--- a/r.sql\n+++ b/r.sql\n@@ -1 +1 @@\n-a\n+b\n"
    changed, deleted = parse_unified_diff(diff)
    assert changed == {"q.sql": [(2, 2)], "r.sql": [(1, 1)]} and deleted == []


def test_git_diff_roundtrip(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    def git(*a): subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True)
    git("init"); git("config", "user.email", "t@t"); git("config", "user.name", "t")
    (r / "m.py").write_text("def f():\n    return 1\n")
    git("add", "."); git("commit", "-m", "init")
    (r / "m.py").write_text("def f():\n    return 2\n")  # change line 2
    changed, _ = parse_unified_diff(git_diff(str(r)))  # working tree vs HEAD
    assert "m.py" in changed and any(lo <= 2 <= hi for lo, hi in changed["m.py"])


def test_impact_cli_on_a_clean_tree(tmp_path):
    """`auryn impact` resolves --path and diffs it before it needs a graph, so a tree with no
    changes exits 0 with a plain message (the command used to die on an unimported `Path`)."""
    r = tmp_path / "repo"
    r.mkdir()
    def git(*a): subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True)
    git("init"); git("config", "user.email", "t@t"); git("config", "user.name", "t")
    (r / "m.py").write_text("def f():\n    return 1\n")
    git("add", "."); git("commit", "-m", "init")
    res = CliRunner().invoke(cli, ["impact", "--path", str(r)])
    assert res.exit_code == 0, res.output
    assert "No changes in the given range." in res.output


def _repo_with_a_change(root):
    """A git repo at `root`: one committed file, then an uncommitted edit to it."""
    root.mkdir(parents=True)
    def git(*a): subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True)
    git("init"); git("config", "user.email", "t@t"); git("config", "user.name", "t")
    (root / "m.py").write_text("def f():\n    return 1\n")
    git("add", "."); git("commit", "-m", "init")
    (root / "m.py").write_text("def f():\n    return 2\n")
    return root


def _stub_the_graph(monkeypatch, indexed):
    """No FalkorDB: the server "holds" the graphs named in `indexed`. Returns what gets selected
    and the changed paths handed to the analysis."""
    selected, analysed = [], []

    class _Client:
        def list_graphs(self):
            return list(indexed)

        def select_graph(self, name):
            selected.append(name)
            return object()

    monkeypatch.setattr("auryn.cli._resolve_connection", lambda *conn: None)
    monkeypatch.setattr("auryn.cli.connect_client", lambda config: _Client())
    monkeypatch.setattr("auryn.core.impact.impact_of_diff",
                        lambda graph, changed, max_depth, repo: analysed.append(sorted(changed)) or {"impact": {}})
    return selected, analysed


@pytest.mark.parametrize("given_by, registered_as", [
    ("--path", None), ("--path", "shared"), ("CLAUDE_PROJECT_DIR", None),
    ("a subfolder of it", "shared"), ("a subfolder of it", None),
])
def test_impact_cli_resolves_the_graph_of_the_repo_it_diffs(tmp_path, monkeypatch, given_by, registered_as):
    """The graph is the one `sync` writes for the repo being diffed -- its registered name, else
    the default for that directory -- wherever the command runs, a subfolder of the repo
    included. It used to be the current folder's, so with `--path` the CI gate crashed on a
    missing graph or, worse, silently checked a different one."""
    from auryn.core.app_config import RepoRegistration, register_repo
    from auryn.core.project import default_graph_name

    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    r = _repo_with_a_change(tmp_path / "repo")
    if registered_as:
        register_repo(RepoRegistration(local_dir=str(r.resolve()), graph_name=registered_as))
    expected = registered_as or default_graph_name(r.resolve())
    selected, _ = _stub_the_graph(monkeypatch, indexed=[expected])

    for folder in (tmp_path / "elsewhere", r / "sub"):
        folder.mkdir()
    monkeypatch.chdir(r / "sub" if given_by == "a subfolder of it" else tmp_path / "elsewhere")
    if given_by == "CLAUDE_PROJECT_DIR":
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(r))
    res = CliRunner().invoke(cli, ["impact", "--json"] + (["--path", str(r)] if given_by == "--path" else []))

    assert res.exit_code == 0, res.output
    assert selected == [expected]


@pytest.mark.parametrize("registered_above", ["a plain folder", "an outer repository"])
def test_impact_cli_never_leaves_the_git_repo_of_its_target(tmp_path, monkeypatch, registered_above):
    """The search for the indexed folder stops at the top of the target's own repository: a plain
    folder registered above it, or an outer repository around a nested one, is never the one
    diffed and checked -- the target's own repo is."""
    from auryn.core.app_config import RepoRegistration, register_repo
    from auryn.core.project import default_graph_name

    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    outer = tmp_path / "outer"
    if registered_above == "an outer repository":
        _repo_with_a_change(outer)
        subprocess.run(["git", "-C", str(outer), "checkout", "--", "m.py"], check=True)  # outer: clean
    inner = _repo_with_a_change(outer / "inner")
    register_repo(RepoRegistration(local_dir=str(outer.resolve()), graph_name="outer-graph"))
    expected = default_graph_name(inner.resolve())
    selected, analysed = _stub_the_graph(monkeypatch, indexed=["outer-graph", expected])

    res = CliRunner().invoke(cli, ["impact", "--json", "--path", str(inner)])

    assert res.exit_code == 0, res.output
    assert (selected, analysed) == ([expected], [["m.py"]])


def test_impact_cli_passes_over_a_folder_registered_only_for_its_hooks(tmp_path, monkeypatch):
    """`auryn hooks enable/disable --repo` registers a folder without indexing it. The search
    wants a folder with a graph, so such a subfolder doesn't narrow the diff: a change elsewhere
    in the repo is still checked, not a "No changes" pass for `--fail-on`."""
    from auryn.core.app_config import set_hook_disabled
    from auryn.core.project import default_graph_name

    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "home"))
    r = _repo_with_a_change(tmp_path / "repo")
    (r / "sub").mkdir()
    set_hook_disabled("some-hook", True, local_dir=str((r / "sub").resolve()))
    expected = default_graph_name(r.resolve())
    selected, analysed = _stub_the_graph(monkeypatch, indexed=[expected])

    res = CliRunner().invoke(cli, ["impact", "--json", "--path", str(r / "sub")])

    assert res.exit_code == 0, res.output
    assert (selected, analysed) == ([expected], [["m.py"]])


def test_impact_cli_says_when_the_repo_is_not_indexed(tmp_path, monkeypatch):
    """A repo with changes but no graph: one line saying so, not the server's "empty key" error
    in a traceback."""
    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "home"))
    r = _repo_with_a_change(tmp_path / "repo")
    _stub_the_graph(monkeypatch, indexed=[])

    res = CliRunner().invoke(cli, ["impact", "--path", str(r)])

    assert res.exit_code == 1
    assert "isn't indexed" in res.output and "Traceback" not in res.output


def test_impact_cli_git_errors_are_one_clean_line(tmp_path, monkeypatch):
    """Not a traceback, and not git's whole usage text: git's own first line and a non-zero exit --
    outside a git repo, and for a revision git doesn't know."""
    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "home"))
    plain = tmp_path / "plain"
    plain.mkdir()
    r = _repo_with_a_change(tmp_path / "repo")

    for args, said in ((["--path", str(plain)], "not a git repository"), (["--path", str(r), "--base", "no-such-ref"], "no-such-ref")):
        res = CliRunner().invoke(cli, ["impact", *args])
        assert res.exit_code == 1
        message = res.output.strip()
        assert message.startswith("Error: couldn't compute git diff") and said in message
        assert len(message.splitlines()) == 1 and "Traceback" not in message


def test_git_toplevel_keeps_a_folder_name_that_ends_in_a_space(tmp_path):
    r = _repo_with_a_change(tmp_path / "repo ")
    assert Path(git_toplevel(str(r))) == r.resolve()


def test_impact_speaks_the_paths_of_a_graph_indexed_from_a_subfolder(tmp_path, monkeypatch):
    """A graph indexed from a subfolder of a repository stores `a.py`, not `service/a.py`. The
    command diffs relative to the indexed folder, or no changed symbol is ever found and the gate
    passes. `git_diff` itself keeps repository paths unless asked: its other callers (the MCP
    tool, the edit hook) may sit in a subfolder of a repo that was indexed from its top."""
    from auryn.core.app_config import RepoRegistration, register_repo

    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "home"))
    r = _repo_with_a_change(tmp_path / "repo")
    (r / "service").mkdir()
    (r / "service" / "a.py").write_text("def g():\n    return 1\n")
    subprocess.run(["git", "-C", str(r), "add", "."], check=True)

    assert sorted(parse_unified_diff(git_diff(str(r / "service"), relative=True))[0]) == ["a.py"]
    assert sorted(parse_unified_diff(git_diff(str(r / "service")))[0]) == ["m.py", "service/a.py"]

    register_repo(RepoRegistration(local_dir=str((r / "service").resolve()), graph_name="svc"))
    selected, analysed = _stub_the_graph(monkeypatch, indexed=["svc"])
    res = CliRunner().invoke(cli, ["impact", "--json", "--path", str(r / "service")])

    assert res.exit_code == 0, res.output
    assert (selected, analysed) == (["svc"], [["a.py"]])


# -- impact_of_diff (integration) ------------------------------------------------------------

pytest.importorskip("redislite.falkordb_client", reason="falkordblite (embedded extra) not installed")

from auryn.core.graph_client import FalkorDBConfig, connect_client


@pytest.fixture
def graph(tmp_path):
    cfg = FalkorDBConfig(embedded=True, db_path=str(tmp_path / "imp.db"), graph_name="svc")
    g = connect_client(cfg).select_graph(cfg.graph_name)
    g.query(
        "CREATE "
        "(gc:Function {id:'orders://orders/db.get_connection', name:'get_connection', file_path:'orders/db.py', line_start:10, line_end:14}),"
        "(place:Function {id:'orders://orders/svc.place_order', name:'place_order', file_path:'orders/svc.py', line_start:20, line_end:40}),"
        "(cancel:Function {id:'orders://orders/svc.cancel_order', name:'cancel_order', file_path:'orders/svc.py', line_start:42, line_end:55}),"
        "(report:Function {id:'orders://orders/report.nightly_totals', name:'nightly_totals', file_path:'orders/report.py', line_start:5, line_end:30}),"
        "(ep:APIEndpoint {id:'orders://orders/ep.post_orders', name:'POST /orders'}),"
        "(test:Function {id:'orders://orders/tests.test_place_order', name:'test_place_order', file_path:'orders/tests/test_svc.py', line_start:8, line_end:20}),"
        "(charge:Function {id:'billing://billing/svc.charge', name:'charge', file_path:'billing/svc.py', line_start:12, line_end:30}) "
        "CREATE (place)-[:CALLS]->(gc), (cancel)-[:CALLS]->(gc), (report)-[:CALLS]->(gc),"
        "(place)-[:EXPOSES_ENDPOINT]->(ep), (test)-[:CALLS]->(place), (charge)-[:CALLS]->(place)"
    )
    return g


def test_impact_of_diff_full_report(graph):
    report = impact_of_diff(graph, {"orders/db.py": [(10, 12)]})
    assert [s["name"] for s in report["changed_symbols"]] == ["get_connection"]
    imp = report["impact"]
    assert set(imp["direct_callers"]) == {"place_order", "cancel_order", "nightly_totals"}
    assert set(imp["transitive"]) == {"charge", "test_place_order"}
    assert imp["api_endpoints_reached"] == ["POST /orders"]
    assert imp["tests_covering"] == ["test_place_order"]
    assert imp["cross_service_consumers"] == ["billing://billing"]
    assert imp["total_affected"] == 5
    assert any("cross-service" in f for f in report["flags"])
    # every affected item carries an id to pivot on
    assert all("id" in a for a in imp["affected"])


def test_impact_of_diff_maps_via_line_overlap_only(graph):
    # a change to lines 42-45 hits cancel_order (42-55), not get_connection (10-14)
    report = impact_of_diff(graph, {"orders/svc.py": [(42, 45)]})
    assert [s["name"] for s in report["changed_symbols"]] == ["cancel_order"]


def test_impact_of_diff_unmapped_file(graph):
    report = impact_of_diff(graph, {"README.md": [(1, 3)]})
    assert report["changed_symbols"] == []
    assert report["unmapped_files"] == ["README.md"]
    assert report["impact"]["total_affected"] == 0


def test_symbols_in_ranges_reports_mapped_paths(graph):
    syms, mapped = symbols_in_ranges(graph, {"orders/db.py": [(10, 12)], "config.yaml": [(1, 2)]})
    assert {s["name"] for s in syms} == {"get_connection"}
    assert mapped == {"orders/db.py"}  # config.yaml mapped nothing


def _seeded(tmp_path, cypher):
    cfg = FalkorDBConfig(embedded=True, db_path=str(tmp_path / "seed.db"), graph_name="seed")
    g = connect_client(cfg).select_graph(cfg.graph_name)
    g.query(cypher)
    return g


# Svc.java: class Svc (lines 3-20) with a field on line 4, `work` (6-9) and `other` (11-13). `run`
# calls `work`; `App`, a constructor, takes a Svc. Each shape ties them to the file the way one
# family of ingestors does.
_CLASS_WITH_METHODS = (
    "CREATE (f:File {id:'java://shop/file/Svc.java', path:'Svc.java'}),"
    "(cls:Class {id:'java://shop/svc.Svc', name:'Svc', line_start:3, line_end:20}),"
    "(work:Method {id:'java://shop/svc.Svc.work()', name:'work', line_start:6, line_end:9}),"
    "(other:Method {id:'java://shop/svc.Svc.other()', name:'other', line_start:11, line_end:13}),"
    "(run:Method {id:'java://shop/app.App.run()', name:'run', line_start:5, line_end:8}),"
    "(app:Method {id:'java://shop/app.App.App(svc)', name:'App', line_start:2, line_end:3}) "
    "CREATE (cls)-[:HAS_METHOD]->(work), (cls)-[:HAS_METHOD]->(other),"
    "(run)-[:CALLS]->(work), (app)-[:DEPENDS_ON]->(cls), "
)
_FILE_HOLDS = {
    "File -DEFINES-> every symbol (Java/Go/Rust/C++)":
        "(f)-[:DEFINES]->(cls), (cls)-[:DEFINED_IN]->(f), (f)-[:DEFINES]->(work), (f)-[:DEFINES]->(other)",
    "the class -DEFINED_IN-> File (Kotlin/PHP/Ruby/Swift/C#)": "(cls)-[:DEFINED_IN]->(f)",
    "a Module -HAS_CLASS-> the class (Python)": "(f)-[:HAS_CLASS]->(cls)",
}


@pytest.mark.parametrize("shape", sorted(_FILE_HOLDS))
def test_impact_of_diff_maps_a_method_change_to_the_method(tmp_path, shape):
    """Regression: a change inside a Java method mapped to its class alone (only `DEFINED_IN` was
    followed, and only types have it), so the report listed whoever takes the class -- not the
    method's callers. The method is found whether the file holds it or only its class does, and
    the class is left out: the change is inside the method's body. A change outside every method
    (a field) is still the class's."""
    g = _seeded(tmp_path, _CLASS_WITH_METHODS + _FILE_HOLDS[shape])

    in_method = impact_of_diff(g, {"Svc.java": [(7, 7)]})
    assert [s["name"] for s in in_method["changed_symbols"]] == ["work"]
    assert in_method["impact"]["direct_callers"] == ["run"]

    on_field = impact_of_diff(g, {"Svc.java": [(4, 4)]})
    assert [s["name"] for s in on_field["changed_symbols"]] == ["Svc"]
    assert on_field["impact"]["direct_callers"] == ["App"]


def test_impact_of_diff_leaves_a_member_to_its_own_file(tmp_path):
    """A member is taken from its type only when it has no file of its own: a C++ method defined
    out of line has lines in the .cpp, which may fall inside the class's span in the header."""
    g = _seeded(tmp_path, (
        "CREATE (:Project {id:'cpp://shop', name:'shop'}),"
        "(h:File {id:'cpp://shop/file/svc.h', path:'svc.h'}), (cc:File {id:'cpp://shop/file/svc.cpp', path:'svc.cpp'}),"
        "(cls:Class {id:'cpp://shop.class.svc', name:'Svc', line_start:1, line_end:10}),"
        "(work:Method {id:'cpp://shop.class.svc.method.work()', name:'work', line_start:5, line_end:8}) "
        "CREATE (h)-[:DEFINES]->(cls), (cls)-[:DEFINED_IN]->(h), (cls)-[:HAS_METHOD]->(work), (cc)-[:DEFINES]->(work)"
    ))
    assert [s["name"] for s in symbols_in_ranges(g, {"svc.h": [(6, 6)]})[0]] == ["Svc"]
    assert [s["name"] for s in symbols_in_ranges(g, {"svc.cpp": [(6, 6)]})[0]] == ["work"]


def _two_repos_with_one_path(tmp_path):
    """Projects `alpha` and `beta`, indexed from /w/repos/alpha and /w/repos/beta, both with a
    `src/app.py`."""
    return _seeded(tmp_path, (
        "CREATE (:Project {id:'python://alpha', name:'alpha', repo_uri:'/w/repos/alpha'}),"
        "(:Project {id:'python://beta', name:'beta', repo_uri:'/w/repos/beta'}),"
        "(ma:Module {id:'python://alpha/module/src.app', file_path:'src/app.py'}),"
        "(mb:Module {id:'python://beta/module/src.app', file_path:'src/app.py'}),"
        "(fa:Function {id:'python://alpha/module/src.app.func.run()', name:'run_alpha', line_start:1, line_end:5}),"
        "(fb:Function {id:'python://beta/module/src.app.func.run()', name:'run_beta', line_start:1, line_end:5}) "
        "CREATE (ma)-[:HAS_FUNCTION]->(fa), (mb)-[:HAS_FUNCTION]->(fb)"
    ))


@pytest.mark.parametrize("path, repo, expected", [
    ("src/app.py", None, ["run_alpha", "run_beta"]),  # nothing says which repo: both, and a flag
    ("alpha/src/app.py", None, ["run_alpha"]),  # a diff taken in the folder above the repos
    ("w/repos/beta/src/app.py", None, ["run_beta"]),
    ("src/app.py", "/w/repos/beta", ["run_beta"]),  # the folder the caller ran git in
    ("gamma/src/app.py", None, []),  # no project was indexed from a `gamma` folder
])
def test_impact_of_diff_scopes_a_path_to_its_project(tmp_path, path, repo, expected):
    """Regression: a diff path was matched exactly and against every project, so a path from a
    diff taken above the repo (`alpha/src/app.py`) mapped nothing, and a path two repos share
    mapped both. A prefix is stripped only toward a project indexed from a folder that ends with
    it, and the match is scoped to that project."""
    report = impact_of_diff(_two_repos_with_one_path(tmp_path), {path: [(2, 2)]}, repo=repo)
    assert sorted(s["name"] for s in report["changed_symbols"]) == expected
    assert any("more than one project" in f for f in report["flags"]) == (len(expected) > 1)


def test_impact_of_diff_reports_a_route_once(tmp_path):
    """Regression: a handler exposes its declared route and, once the HTTP stitch matched a call
    to it, the call site's endpoint as well -- named with `{param}` -- so a route with a path
    parameter was reported twice."""
    g = _seeded(tmp_path, (
        "CREATE (h:Function {id:'python://orders/module/api.func.cancel(order_id)', name:'cancel', file_path:'api.py', line_start:1, line_end:5}),"
        "(own:APIEndpoint {id:'pyapi://python://orders/module/api.func.cancel(order_id)#POST:/orders/{order_id}/cancel', name:'POST /orders/{order_id}/cancel'}),"
        "(call:APIEndpoint {id:'extern://http/POST:/orders/{param}/cancel', name:'POST /orders/{param}/cancel'}) "
        "CREATE (h)-[:EXPOSES_ENDPOINT]->(own), (h)-[:EXPOSES_ENDPOINT]->(call)"
    ))
    report = impact_of_diff(g, {"api.py": [(2, 2)]})
    assert report["impact"]["api_endpoints_reached"] == ["POST /orders/{order_id}/cancel"]


def test_impact_of_diff_keeps_dot_separated_ids_in_their_project(tmp_path):
    """C++ and PHP ids go on from the project's id with a `.` (`cpp://shop.class.svc`), so cutting
    an id at its first `/` made each class its own "service" and flagged every caller in the same
    repo as cross-service."""
    g = _seeded(tmp_path, (
        "CREATE (:Project {id:'cpp://shop', name:'shop'}), (:Project {id:'cpp://billing', name:'billing'}),"
        "(work:Method {id:'cpp://shop.class.svc.method.work()', name:'work', file_path:'svc.cpp', line_start:5, line_end:8}),"
        "(run:Method {id:'cpp://shop.class.app.method.run()', name:'run', file_path:'app.cpp', line_start:1, line_end:4}),"
        "(pay:Function {id:'cpp://billing.func.pay()', name:'pay', file_path:'pay.cpp', line_start:1, line_end:4}) "
        "CREATE (run)-[:CALLS]->(work), (pay)-[:CALLS]->(work)"
    ))
    report = impact_of_diff(g, {"svc.cpp": [(6, 6)]})
    assert report["impact"]["cross_service_consumers"] == ["cpp://billing"]
