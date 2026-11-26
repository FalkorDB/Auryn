"""Regression for #857: a deep (semantic) pass that FAILS must not be silent. The CLI summary surfaces it
loudly and the command exits non-zero, so a shallow (`depth=fast`) graph isn't queried as if it were a
complete, deep one. The trigger in the field was a FalkorDB `Query timed out` on a large Python repo; the
bug was purely that Auryn caught it, exited 0, and said nothing."""

from __future__ import annotations

from auryn.cli import _echo_ingest_report
from auryn.core import ingest
from auryn.core.graph_client import GraphWriter


class _Report:
    """Minimal stand-in exposing what `_echo_ingest_report` reads: project_name, commit_sha, to_dict()."""

    project_name = "django"
    commit_sha = "abc123def456789"

    def __init__(self, langs):
        self._langs = langs

    def to_dict(self):
        zero = dict(nodes_added=0, nodes_removed=0, nodes_updated=0,
                    edges_added=0, edges_removed=0, edges_updated=0)
        return {"elapsed_seconds": 1.0, "languages": self._langs, "unsupported_detected": [],
                "enrichments": {}, "totals": zero}


def _lang(name, deep_status):
    return {"language": name, "nodes_added": 100, "nodes_removed": 0, "nodes_updated": 0,
            "edges_added": 200, "edges_removed": 0, "edges_updated": 0, "files_parsed": 50,
            "files_cached": 0, "deep_status": deep_status}


def test_deep_failure_is_surfaced_and_returned(capsys):
    failed = _echo_ingest_report(_Report([_lang("python", "error")]))
    assert failed == ["python"]                        # this is what drives the caller's non-zero exit
    combined = "".join(capsys.readouterr())            # (stdout + stderr)
    assert "FAILED" in combined and "shallow" in combined.lower()   # loud, not silent
    assert "timed out" in combined.lower()             # actionable: the FalkorDB-timeout hint


def test_deep_success_is_quiet_and_returns_empty(capsys):
    assert _echo_ingest_report(_Report([_lang("python", "inline")])) == []
    assert "FAILED" not in "".join(capsys.readouterr())


def test_non_error_deep_status_is_noted_but_not_a_failure(capsys):
    # async/background/skipped are normal outcomes -> shown for context, but not failures.
    assert _echo_ingest_report(_Report([_lang("python", "async")])) == []
    out = capsys.readouterr().out
    assert "[deep: async]" in out and "FAILED" not in out


def test_deep_pass_exception_leaves_error_status_and_a_shallow_graph(graph, tmp_path, monkeypatch):
    # Recreate the #857 trigger end-to-end: the deep pass raising (as a FalkorDB `Query timed out` does)
    # is captured as deep_status="error" with the fast graph still landed -- not swallowed as success.
    from auryn.ingestors.python import git_sync as py_git_sync

    def _timeout(*args, **kwargs):
        raise RuntimeError("Query timed out")

    monkeypatch.setattr(py_git_sync, "jedi_language_sync", _timeout)
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.py").write_text("def f():\n    return g()\n\n\ndef g():\n    return 1\n")

    report = ingest.ingest_directory(GraphWriter(graph), str(root), cache_root=tmp_path / "cache", deep="inline")
    py = next(lang for lang in report.languages if lang.language == "python")
    assert py.deep_status == "error"   # the deep failure is captured (and will exit non-zero in the CLI)...
    assert py.result is not None       # ...while the fast graph still landed (shallow but usable)
    assert _echo_ingest_report(report) == ["python"]   # ...and the CLI surfaces + signals it


def test_background_deep_failure_is_surfaced_by_the_scope(graph, tmp_path, monkeypatch):
    # #857 review: a `--deep background` pass that fails must still be surfaced. The report is built
    # (deep_status="background") before the thread finishes; deep_failure_scope() collects THIS
    # ingest's deep threads, joins them on exit, and exposes the failures -- so the CLI exits non-zero
    # instead of exiting 0 on a silently shallow graph. Thread-local + caller-owned, so a concurrent
    # ingest or the embed-after-deep waiter can't consume or mis-attribute the signal.
    from auryn.ingestors.python import git_sync as py_git_sync

    def _timeout(*args, **kwargs):
        raise RuntimeError("Query timed out")

    monkeypatch.setattr(py_git_sync, "jedi_language_sync", _timeout)
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.py").write_text("def f():\n    return 1\n")

    with ingest.deep_failure_scope() as bg:
        report = ingest.ingest_directory(GraphWriter(graph), str(root),
                                         cache_root=tmp_path / "cache", deep="background")
        py = next(lang for lang in report.languages if lang.language == "python")
        assert py.deep_status == "background"         # inside the scope the thread may still be running

    assert bg["failed"] == {"python"}                 # on exit the scope joined + surfaced the failure
    assert _echo_ingest_report(report, background_failures=bg["failed"]) == ["python"]


def test_background_deep_success_surfaces_nothing(graph, tmp_path):
    # A clean `--deep background` pass leaves the scope's failure set empty -> no false FAILED/exit.
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.py").write_text("def f():\n    return g()\n\n\ndef g():\n    return 1\n")
    with ingest.deep_failure_scope() as bg:
        ingest.ingest_directory(GraphWriter(graph), str(root), cache_root=tmp_path / "cache", deep="background")
    assert bg["failed"] == set()
