"""Diff/PR-aware impact: the unified-diff parser (pure) + impact_of_diff over a seeded
two-service graph, plus a real git round-trip. Embedded-FalkorDB tests skip without the extra."""

from __future__ import annotations

import subprocess

import pytest
from click.testing import CliRunner

from auryn.cli import cli
from auryn.core.impact import git_diff, impact_of_diff, parse_unified_diff, symbols_in_ranges


# -- parse_unified_diff (pure) ---------------------------------------------------------------


def test_parse_modified_file_hunk():
    diff = "diff --git a/orders/db.py b/orders/db.py\n--- a/orders/db.py\n+++ b/orders/db.py\n@@ -10,3 +10,2 @@\n-old\n+new\n"
    changed, deleted = parse_unified_diff(diff)
    assert changed == {"orders/db.py": [(10, 11)]}  # +10,2 -> lines 10..11
    assert deleted == []


def test_parse_count_omitted_is_single_line():
    changed, _ = parse_unified_diff("--- a/x.py\n+++ b/x.py\n@@ -5 +7 @@\n-a\n+b\n")
    assert changed == {"x.py": [(7, 7)]}


def test_parse_new_file():
    diff = "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1,4 @@\n+a\n+b\n"
    changed, deleted = parse_unified_diff(diff)
    assert changed == {"new.py": [(1, 4)]} and deleted == []


def test_parse_deleted_file_is_full_span():
    diff = "diff --git a/gone.py b/gone.py\ndeleted file mode 100644\n--- a/gone.py\n+++ /dev/null\n@@ -1,5 +0,0 @@\n-x\n"
    changed, deleted = parse_unified_diff(diff)
    assert deleted == ["gone.py"]
    assert changed["gone.py"] == [(0, 10**9)]  # whole file -> every symbol counts as changed


def test_parse_multiple_hunks_and_files():
    diff = (
        "--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-x\n+y\n@@ -20,0 +21,3 @@\n+n\n"
        "--- a/b.py\n+++ b/b.py\n@@ -5,2 +5,0 @@\n-z\n"
    )
    changed, _ = parse_unified_diff(diff)
    assert changed["a.py"] == [(1, 1), (21, 23)]
    assert changed["b.py"] == [(5, 5)]  # +5,0 deletion -> anchored at 5


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


@pytest.mark.parametrize("given_by, registered_as", [("--path", None), ("--path", "shared"), ("CLAUDE_PROJECT_DIR", None)])
def test_impact_cli_resolves_the_graph_of_the_repo_it_diffs(tmp_path, monkeypatch, given_by, registered_as):
    """The graph is the one `sync` writes for the repo being diffed -- its registered name, else
    the default for that directory -- wherever the command runs. It used to be the current
    folder's, so with `--path` the CI gate crashed on a missing graph or, worse, silently checked
    a different one."""
    from auryn.core.app_config import RepoRegistration, register_repo
    from auryn.core.project import default_graph_name

    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    r = tmp_path / "repo"
    r.mkdir()
    def git(*a): subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True)
    git("init"); git("config", "user.email", "t@t"); git("config", "user.name", "t")
    (r / "m.py").write_text("def f():\n    return 1\n")
    git("add", "."); git("commit", "-m", "init")
    (r / "m.py").write_text("def f():\n    return 2\n")  # an uncommitted change to analyse
    if registered_as:
        register_repo(RepoRegistration(local_dir=str(r.resolve()), graph_name=registered_as))

    selected = []

    class _Client:
        def select_graph(self, name):
            selected.append(name)
            return object()

    monkeypatch.setattr("auryn.cli._resolve_connection", lambda *conn: None)
    monkeypatch.setattr("auryn.cli.connect_client", lambda config: _Client())
    monkeypatch.setattr("auryn.core.impact.impact_of_diff", lambda graph, changed, max_depth: {"impact": {}})

    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(tmp_path / "elsewhere")
    if given_by == "CLAUDE_PROJECT_DIR":
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(r))
    res = CliRunner().invoke(cli, ["impact", "--json"] + (["--path", str(r)] if given_by == "--path" else []))

    assert res.exit_code == 0, res.output
    assert selected == [registered_as or default_graph_name(r.resolve())]


def test_impact_cli_outside_a_git_repo_is_a_clean_error(tmp_path):
    """Not a traceback: a one-line message and a non-zero exit, like the MCP tool's error."""
    res = CliRunner().invoke(cli, ["impact", "--path", str(tmp_path)])
    assert res.exit_code == 1
    assert "couldn't compute git diff" in res.output and "Traceback" not in res.output


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
