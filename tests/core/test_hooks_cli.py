"""`auryn hooks` CLI + the --only-hook/--skip-hook / enrich --skip validation.

Driven as a subprocess so each invocation gets a fresh interpreter (real usage, and it sidesteps
the in-process load-once guard for local-hook discovery). AURYN_HOME is a temp dir, so nothing
touches the developer's real ~/.auryn.
"""

from __future__ import annotations

import os
import subprocess
import sys


def _run(tmp_home, *args):
    env = {**os.environ, "AURYN_HOME": str(tmp_home)}
    return subprocess.run([sys.executable, "-m", "auryn", *args],
                          capture_output=True, text=True, env=env)


def test_hooks_new_scaffolds_and_list_shows_it(tmp_path):
    home = tmp_path / "cg"
    r = _run(home, "hooks", "new", "link-tests")
    assert r.returncode == 0, r.stderr
    created = home / "hooks" / "link_tests.py"
    assert created.exists()
    assert 'NAME = "link-tests"' in created.read_text()  # registered name == the typed name

    r = _run(home, "hooks", "list")
    assert r.returncode == 0, r.stderr
    assert "link-tests" in r.stdout
    assert "local" in r.stdout and "enabled" in r.stdout


def test_hooks_disable_enable_persists_to_config(tmp_path):
    home = tmp_path / "cg"
    _run(home, "hooks", "new", "link-tests")

    assert _run(home, "hooks", "disable", "link-tests").returncode == 0
    assert 'disabled = [' in (home / "config.toml").read_text()
    assert "disabled" in _run(home, "hooks", "list").stdout

    assert _run(home, "hooks", "enable", "link-tests").returncode == 0
    # re-enabled -> no longer in the list output as disabled
    listing = _run(home, "hooks", "list").stdout
    assert "link-tests" in listing
    assert "disabled" not in listing


def test_hooks_disable_unknown_name_errors(tmp_path):
    r = _run(tmp_path / "cg", "hooks", "disable", "does-not-exist")
    assert r.returncode != 0
    assert "unknown hook" in (r.stdout + r.stderr).lower()


def test_ingest_rejects_unknown_hook_name_before_connecting(tmp_path):
    # Validation happens before any backend connection, so this fails fast with a clean message.
    (tmp_path / "src").mkdir()
    r = _run(tmp_path / "cg", "ingest", "dir", str(tmp_path / "src"), "--only-hook", "nope")
    assert r.returncode != 0
    assert "unknown hook" in (r.stdout + r.stderr).lower()


def test_enrich_rejects_unknown_skip_name(tmp_path):
    (tmp_path / "src").mkdir()
    r = _run(tmp_path / "cg", "enrich", str(tmp_path / "src"), "--skip", "nope")
    assert r.returncode != 0
    assert "unknown enrichment" in (r.stdout + r.stderr).lower()


def test_hooks_list_reports_broken_file(tmp_path):
    home = tmp_path / "cg"
    hooks = home / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "bad.py").write_text("def run(w, r, t):\n    return None\nraise ValueError('boom')\n")
    r = _run(home, "hooks", "list")
    assert r.returncode == 0, r.stderr
    assert "BROKEN" in (r.stdout + r.stderr)
    assert "boom" in (r.stdout + r.stderr)


def test_hooks_new_refuses_overwrite_without_force(tmp_path):
    home = tmp_path / "cg"
    assert _run(home, "hooks", "new", "dup").returncode == 0
    r2 = _run(home, "hooks", "new", "dup")
    assert r2.returncode != 0
    assert "already exists" in (r2.stdout + r2.stderr).lower()
    assert _run(home, "hooks", "new", "dup", "--force").returncode == 0  # --force overwrites


def test_hooks_disable_enable_per_repo_scope(tmp_path):
    home = tmp_path / "cg"
    proj = tmp_path / "proj"
    proj.mkdir()
    _run(home, "hooks", "new", "link-tests")

    # disable for this repo only
    r = _run(home, "hooks", "disable", "link-tests", "--repo", str(proj))
    assert r.returncode == 0, r.stderr
    cfg = (home / "config.toml").read_text()
    assert "disabled_hooks" in cfg and str(proj.resolve()) in cfg  # persisted per-repo

    # list --repo reflects the per-repo disable; the install-wide view does not
    assert "disabled" in _run(home, "hooks", "list", "--repo", str(proj)).stdout
    assert "disabled" not in _run(home, "hooks", "list").stdout

    # enable per-repo removes it again
    assert _run(home, "hooks", "enable", "link-tests", "--repo", str(proj)).returncode == 0
    assert "disabled" not in _run(home, "hooks", "list", "--repo", str(proj)).stdout


def test_enrich_skip_reduces_the_set(tmp_path):
    # Skipping every registered enrichment leaves nothing to run -> clean error, before any connect.
    # Discover the full set so this stays correct as enrichments are added (orm-tables,
    # python-routes, http-endpoints, ...).
    from auryn.core.registry import discover_graph_enrichments

    (tmp_path / "src").mkdir()
    skips = [arg for name in discover_graph_enrichments() for arg in ("--skip", name)]
    r = _run(tmp_path / "cg", "enrich", str(tmp_path / "src"), *skips)
    assert r.returncode != 0
    assert "no enrichments" in (r.stdout + r.stderr).lower()


def test_ingest_repo_rejects_unknown_hook_name(tmp_path):
    # ingest repo validates hook names before any clone/connect, same as ingest dir.
    r = _run(tmp_path / "cg", "ingest", "repo", "/nonexistent/repo.git", "--only-hook", "nope")
    assert r.returncode != 0
    assert "unknown hook" in (r.stdout + r.stderr).lower()
