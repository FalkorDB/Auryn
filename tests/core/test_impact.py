"""Diff/PR-aware impact: the unified-diff parser (pure) + impact_of_diff over a seeded
two-service graph, plus a real git round-trip. Embedded-FalkorDB tests skip without the extra."""

from __future__ import annotations

import subprocess

import pytest

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
