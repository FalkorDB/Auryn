"""Integration tests for the multi-language ingestion endpoint (core/ingest.py)
against the real `graph` fixture and a local git repo built with GitPython -- no
network needed. Exercises the dispatch/report layer; each language's own ingestion
is covered by its own suite.
"""

from __future__ import annotations

import git
import pytest

from auryn.core import ingest
from auryn.core.graph_client import GraphWriter

_WIDGET_CS = "namespace Acme { public class Widget { public int Id { get; set; } } }"
_HR_PKB = """
CREATE OR REPLACE PACKAGE BODY hr IS
  PROCEDURE hire(p_name IN VARCHAR2) IS BEGIN INSERT INTO employees(name) VALUES (p_name); END;
END hr;
"""


def _make_repo(root, files: dict[str, str]) -> str:
    repo = git.Repo.init(str(root), initial_branch="main")
    with repo.config_writer() as cw:
        cw.set_value("user", "email", "t@e.com")
        cw.set_value("user", "name", "T")
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    repo.index.add(list(files))
    return repo.index.commit("init").hexsha


def _make_dir(root, files: dict[str, str]) -> None:
    """A plain directory of files -- no git init, no commit."""
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def test_ingests_every_supported_language_in_a_mixed_repo(graph, tmp_path):
    root = tmp_path / "MixedRepo"
    commit = _make_repo(
        root,
        {
            "src/Widget.cs": _WIDGET_CS,
            "db/hr.pkb": _HR_PKB,
            "README.md": "# docs",
            "web/app.ts": "export function boot(): void {}",
            "cmd/main.go": "package main\nfunc main() {}",
            "legacy/old.rb": "def hi\nend\n",
            "ui/Counter.razor": "<h1>@count</h1>\n@code { int count = 0; }\n",
        },
    )

    report = ingest.ingest_repository(GraphWriter(graph), str(root), cache_root=tmp_path / "cache")

    assert report.commit_sha == commit
    assert report.project_name == "MixedRepo"  # derived from the repo path
    ingested = {r.language: r for r in report.languages if r.result is not None}
    assert set(ingested) == {"csharp", "plsql", "typescript", "go", "ruby"}
    assert ingested["csharp"].result.nodes_added > 0
    assert ingested["plsql"].result.nodes_added > 0
    assert ingested["typescript"].result.nodes_added > 0
    assert ingested["go"].result.nodes_added > 0
    assert ingested["ruby"].result.nodes_added > 0

    # All five subgraphs actually landed, each under its own project root.
    assert graph.query("MATCH (c:Class {name:'Widget'}) RETURN c").result_set != []
    assert graph.query("MATCH (p:Procedure {id:'plsql://MixedRepo.package.hr.procedure.hire'}) RETURN p").result_set != []
    assert graph.query("MATCH (m:Module {name:'app.ts'}) RETURN m").result_set != []
    assert graph.query("MATCH (p:Package {name:'main'}) RETURN p").result_set != []
    assert graph.query("MATCH (f:Function {name:'hi'}) RETURN f").result_set != []  # the ruby method

    # The Blazor (.razor) file is reported as detected-but-unsupported, not silently ignored
    # (ruby, .kt, .swift, .sh are now first-class -- .razor has no ingestor).
    unsupported = {u["language"] for u in report.unsupported_detected}
    assert "blazor" in unsupported


def test_report_totals_sum_the_per_language_deltas(graph, tmp_path):
    root = tmp_path / "Repo"
    _make_repo(root, {"src/Widget.cs": _WIDGET_CS, "db/hr.pkb": _HR_PKB})

    report = ingest.ingest_repository(GraphWriter(graph), str(root), cache_root=tmp_path / "cache")

    totals = report.to_dict()["totals"]
    expected = sum(r.result.nodes_added for r in report.languages if r.result is not None)
    assert totals["nodes_added"] == expected
    assert totals["nodes_added"] > 0


def test_repo_with_no_supported_files_is_a_clean_noop(graph, tmp_path):
    root = tmp_path / "DocsOnly"
    _make_repo(root, {"README.md": "# docs", "notes/todo.txt": "later"})

    report = ingest.ingest_repository(GraphWriter(graph), str(root), cache_root=tmp_path / "cache")

    assert [r for r in report.languages if r.result is not None] == []
    assert report.to_dict()["languages_ingested"] == []


def test_resyncing_the_same_commit_is_a_zero_delta_noop(graph, tmp_path):
    root = tmp_path / "Repo"
    _make_repo(root, {"src/Widget.cs": _WIDGET_CS, "db/hr.pkb": _HR_PKB})
    cache = tmp_path / "cache"

    ingest.ingest_repository(GraphWriter(graph), str(root), cache_root=cache)
    again = ingest.ingest_repository(GraphWriter(graph), str(root), cache_root=cache)

    assert again.to_dict()["totals"] == {
        "nodes_added": 0,
        "nodes_updated": 0,
        "nodes_removed": 0,
        "edges_added": 0,
        "edges_updated": 0,
        "edges_removed": 0,
    }


def test_bad_ref_raises_valueerror(graph, tmp_path):
    root = tmp_path / "Repo"
    _make_repo(root, {"db/hr.pkb": _HR_PKB})

    with pytest.raises(ValueError, match="does not match"):
        ingest.ingest_repository(
            GraphWriter(graph), str(root), ref="no-such-branch", cache_root=tmp_path / "cache"
        )


# --- ingest_directory: the same pipeline over a plain local directory (no git) ---


def test_ingest_directory_ingests_every_supported_language(graph, tmp_path):
    root = tmp_path / "LocalWidgets"
    _make_dir(
        root,
        {
            "src/Widget.cs": _WIDGET_CS,
            "db/hr.pkb": _HR_PKB,
            "README.md": "# docs",
            "web/app.ts": "export function boot(): void {}",
            "cmd/main.go": "package main\nfunc main() {}",
            "legacy/old.rb": "def hi\nend\n",
            "ui/Counter.razor": "<h1>@count</h1>\n@code { int count = 0; }\n",
        },
    )

    report = ingest.ingest_directory(GraphWriter(graph), str(root), cache_root=tmp_path / "cache")

    assert report.project_name == "LocalWidgets"  # derived from the directory name
    assert report.repo_uri == str(root.resolve())  # the abspath is recorded as the source
    assert report.commit_sha  # a tree hash stands in for a commit sha
    ingested = {r.language: r for r in report.languages if r.result is not None}
    assert set(ingested) == {"csharp", "plsql", "typescript", "go", "ruby"}
    assert ingested["csharp"].result.nodes_added > 0
    assert ingested["plsql"].result.nodes_added > 0
    assert ingested["typescript"].result.nodes_added > 0
    assert ingested["go"].result.nodes_added > 0
    assert ingested["ruby"].result.nodes_added > 0

    # All five subgraphs actually landed, each under its own project root.
    assert graph.query("MATCH (c:Class {name:'Widget'}) RETURN c").result_set != []
    assert graph.query(
        "MATCH (p:Procedure {id:'plsql://LocalWidgets.package.hr.procedure.hire'}) RETURN p"
    ).result_set != []
    assert graph.query("MATCH (m:Module {name:'app.ts'}) RETURN m").result_set != []
    assert graph.query("MATCH (p:Package {name:'main'}) RETURN p").result_set != []
    assert graph.query("MATCH (f:Function {name:'hi'}) RETURN f").result_set != []  # the ruby method

    # The Blazor (.razor) file is reported as detected-but-unsupported, not silently ignored
    # (ruby, .kt, .swift, .sh are now first-class -- .razor has no ingestor).
    assert "blazor" in {u["language"] for u in report.unsupported_detected}


def test_reingesting_an_unchanged_directory_is_a_zero_delta_noop(graph, tmp_path):
    root = tmp_path / "LocalWidgets"
    _make_dir(root, {"src/Widget.cs": _WIDGET_CS, "db/hr.pkb": _HR_PKB})
    cache = tmp_path / "cache"

    first = ingest.ingest_directory(GraphWriter(graph), str(root), cache_root=cache)
    again = ingest.ingest_directory(GraphWriter(graph), str(root), cache_root=cache)

    assert first.commit_sha == again.commit_sha  # unchanged content -> same tree hash
    assert again.to_dict()["totals"] == {
        "nodes_added": 0,
        "nodes_updated": 0,
        "nodes_removed": 0,
        "edges_added": 0,
        "edges_updated": 0,
        "edges_removed": 0,
    }


def test_editing_a_file_reingests_with_a_precise_delta(graph, tmp_path):
    root = tmp_path / "LocalWidgets"
    _make_dir(root, {"src/Widget.cs": _WIDGET_CS, "db/hr.pkb": _HR_PKB})
    cache = tmp_path / "cache"

    first = ingest.ingest_directory(GraphWriter(graph), str(root), cache_root=cache)

    # Add a property to the C# class -> the tree hash changes and the class re-syncs.
    (root / "src" / "Widget.cs").write_text(
        "namespace Acme { public class Widget { public int Id { get; set; } public string Name { get; set; } } }"
    )
    second = ingest.ingest_directory(GraphWriter(graph), str(root), cache_root=cache)

    assert second.commit_sha != first.commit_sha
    assert second.to_dict()["totals"]["nodes_added"] > 0  # the new Name property


def test_a_sync_that_dies_mid_apply_is_healed_by_the_next_one(graph, tmp_path, monkeypatch):
    """End to end: the write of the edges fails (a timeout, a kill, an OOM). The next ingest of the
    SAME content must finish the job -- it used to find the commit already stamped, report a clean
    no-op, and leave the graph without its edges for good."""
    root = tmp_path / "app"
    _make_dir(root, {"svc.py": "def helper():\n    return 1\n\n\ndef run():\n    return helper()\n"})
    cache = tmp_path / "cache"

    def killed(self, edges):
        raise RuntimeError("killed mid-apply")

    with monkeypatch.context() as m:
        m.setattr(GraphWriter, "merge_edges", killed)
        broken = ingest.ingest_directory(GraphWriter(graph), str(root), cache_root=cache, deep="off")
    assert "killed mid-apply" in str(broken.to_dict())  # reported, not swallowed

    ingest.ingest_directory(GraphWriter(graph), str(root), cache_root=cache, deep="off")

    assert graph.query(
        "MATCH (:Function {name: 'run'})-[r:CALLS]->(:Function {name: 'helper'}) RETURN count(r)"
    ).result_set == [[1]]


def test_a_failed_sync_is_healed_by_going_back_to_the_previous_content(graph, tmp_path, monkeypatch):
    """The sync to new content fails midway and the tree then goes BACK (a reset, a revert). The
    graph still holds the stamp of... nothing: had it kept the previous commit's, this re-ingest
    of the previous content would short-circuit over the half-applied change. (This one passes
    on the old stamp-first order too; it pins down the alternative of keeping the OLD stamp
    until the end, which an earlier version of this change did.)"""
    root = tmp_path / "app"
    v1 = "def helper():\n    return 1\n\n\ndef run():\n    return helper()\n"
    _make_dir(root, {"svc.py": v1})
    cache = tmp_path / "cache"
    ingest.ingest_directory(GraphWriter(graph), str(root), cache_root=cache, deep="off")

    def killed(self, edges):
        raise RuntimeError("killed mid-apply")

    (root / "svc.py").write_text(v1 + "\n\ndef added():\n    return run()\n")
    with monkeypatch.context() as m:
        m.setattr(GraphWriter, "merge_edges", killed)
        ingest.ingest_directory(GraphWriter(graph), str(root), cache_root=cache, deep="off")
    assert graph.query("MATCH (n:Function {name: 'added'}) RETURN count(n)").result_set == [[1]]  # half-applied

    (root / "svc.py").write_text(v1)
    ingest.ingest_directory(GraphWriter(graph), str(root), cache_root=cache, deep="off")

    assert graph.query("MATCH (n:Function {name: 'added'}) RETURN count(n)").result_set == [[0]]


def test_ingest_directory_missing_path_raises_valueerror(graph, tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        ingest.ingest_directory(GraphWriter(graph), str(tmp_path / "nope"))


def test_ingest_directory_file_path_raises_valueerror(graph, tmp_path):
    a_file = tmp_path / "a.cs"
    a_file.write_text("x")

    with pytest.raises(ValueError, match="not a directory"):
        ingest.ingest_directory(GraphWriter(graph), str(a_file))
