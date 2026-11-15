"""run_post_hooks -- the shared post-hook (graph enrichment) pass.

This is the single seam BOTH ingest paths flow through: `ingest_repository` (full clone) and
`ingest_directory` (the target of `auryn sync` -> commit hook / save watcher / manual resync).
So proving it here proves hooks run -- and honour the toggle -- on the incremental resync path
too, not only on a first full ingest.
"""

from __future__ import annotations

from auryn.core import registry
from auryn.core.graph_client import GraphWriter
from auryn.core.ingest import run_post_hooks
from auryn.core.registry import GraphEnrichment


class _StubSource:
    """A repo stand-in so the real shipped hooks (orm-tables/python-routes) find no input
    and no-op, keeping the assertions about the test hooks clean."""

    def list_files(self, sha, suffix, excluded):
        return {}

    def read_blob(self, sha):
        return b""


def test_run_post_hooks_runs_isolates_failures_and_honours_toggle(graph):
    calls: list[str] = []

    def ok(writer, repo, target_sha):
        calls.append("ok")
        return {"ran": True}

    def boom(writer, repo, target_sha):
        raise RuntimeError("kaboom")

    saved_flag = registry._local_hooks_loaded
    try:
        registry._local_hooks_loaded = True  # don't pull in ~/.auryn/hooks during the test
        registry.register_graph_enrichment(GraphEnrichment("zz-test-ok", ok, order=999))
        registry.register_graph_enrichment(GraphEnrichment("zz-test-boom", boom, order=998))

        # both enabled: enabled hook runs, a failing hook is isolated (recorded, not raised)
        reports = run_post_hooks(GraphWriter(graph), _StubSource(), "sha")
        assert reports["zz-test-ok"] == {"ran": True}
        assert "error" in reports["zz-test-boom"] and "kaboom" in reports["zz-test-boom"]["error"]
        assert calls == ["ok"]

        # disabled (config) suppresses it -- this is what makes a per-repo/install-wide disable
        # take effect on the resync path
        reports = run_post_hooks(GraphWriter(graph), _StubSource(), "sha", disabled={"zz-test-ok"})
        assert "zz-test-ok" not in reports

        # skip (per-run) suppresses it too
        reports = run_post_hooks(GraphWriter(graph), _StubSource(), "sha", skip={"zz-test-ok"})
        assert "zz-test-ok" not in reports

        # only (per-run) runs exactly that one, ignoring the rest
        reports = run_post_hooks(GraphWriter(graph), _StubSource(), "sha", only={"zz-test-ok"})
        assert set(reports) == {"zz-test-ok"}
    finally:
        registry._ENRICHMENT_REGISTRY.pop("zz-test-ok", None)
        registry._ENRICHMENT_REGISTRY.pop("zz-test-boom", None)
        registry._local_hooks_loaded = saved_flag


def test_local_hook_runs_via_ingest_directory_and_honours_toggle(graph, tmp_path, monkeypatch):
    """End-to-end through the REAL ingest entrypoint (what `auryn sync` calls): a local
    drop-in is discovered and run, a per-run --skip suppresses it, and a config disable
    (resolved via _source_local_dir -> effective_disabled_hooks) suppresses it. Re-ingesting
    the same dir exercises the resync path -- the hook fires again."""
    from auryn.core.app_config import set_hook_disabled
    from auryn.core.ingest import ingest_directory

    home = tmp_path / "cg"
    hooks = home / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "probe_hook.py").write_text(
        'NAME = "probe-hook"\n'
        "def run(writer, repo, target_sha):\n"
        '    return {"ran": True}\n'
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "m.py").write_text("def foo():\n    return 1\n")
    monkeypatch.setenv("AURYN_HOME", str(home))

    writer = GraphWriter(graph)
    saved_flag = registry._local_hooks_loaded

    def rescan():  # each ingest is normally a fresh process; force a re-glob of the hooks dir
        registry._local_hooks_loaded = False

    try:
        # 1. discovered + run through the real ingest path
        rescan()
        rep = ingest_directory(writer, str(repo), deep="off", auto_install=False)
        assert rep.enrichments.get("probe-hook") == {"ran": True}

        # 2. re-ingest (resync path) -> still runs
        rescan()
        rep = ingest_directory(writer, str(repo), deep="off", auto_install=False)
        assert rep.enrichments.get("probe-hook") == {"ran": True}

        # 3. per-run --skip-hook suppresses it (no persistence)
        rescan()
        rep = ingest_directory(writer, str(repo), deep="off", auto_install=False,
                               enrichment_skip=("probe-hook",))
        assert "probe-hook" not in rep.enrichments

        # 4. install-wide config disable suppresses it (config -> effective_disabled_hooks)
        set_hook_disabled("probe-hook", True)
        rescan()
        rep = ingest_directory(writer, str(repo), deep="off", auto_install=False)
        assert "probe-hook" not in rep.enrichments
    finally:
        registry._ENRICHMENT_REGISTRY.pop("probe-hook", None)
        registry._local_hooks_loaded = saved_flag


def test_only_hook_runs_exactly_that_one_via_ingest_directory(graph, tmp_path, monkeypatch):
    """--only-hook through the real path runs the named hook and drops the rest (incl. plugins)."""
    from auryn.core.ingest import ingest_directory

    home = tmp_path / "cg"
    (home / "hooks").mkdir(parents=True)
    (home / "hooks" / "probe_hook.py").write_text(
        'NAME = "probe-hook"\ndef run(writer, repo, target_sha):\n    return {"ran": True}\n'
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "m.py").write_text("x = 1\n")
    monkeypatch.setenv("AURYN_HOME", str(home))

    saved_flag = registry._local_hooks_loaded
    try:
        registry._local_hooks_loaded = False
        rep = ingest_directory(GraphWriter(graph), str(repo), deep="off", auto_install=False,
                               enrichment_only=("probe-hook",))
        assert set(rep.enrichments) == {"probe-hook"}
    finally:
        registry._ENRICHMENT_REGISTRY.pop("probe-hook", None)
        registry._local_hooks_loaded = saved_flag
