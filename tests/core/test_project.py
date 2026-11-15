"""Repo/graph-name resolution: which directory a command targets and the graph it maps to."""

from __future__ import annotations

from pathlib import Path

from auryn.core.project import default_graph_name, graph_name_for_repo, resolve_project_root


def test_resolve_prefers_explicit_path_over_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "elsewhere"))
    assert resolve_project_root(str(tmp_path)) == tmp_path.resolve()


def test_resolve_uses_claude_project_dir_when_no_path(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    assert resolve_project_root() == tmp_path.resolve()


def test_resolve_falls_back_to_cwd(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    assert resolve_project_root() == tmp_path.resolve()


def test_default_graph_name_is_stable_and_path_unique():
    name = default_graph_name(Path("/work/acme-app"))
    assert name == default_graph_name(Path("/work/acme-app"))  # deterministic
    assert name.startswith("acme-app-")
    # same basename, different absolute path -> different graph (no collision)
    assert name != default_graph_name(Path("/other/acme-app"))


def test_graph_name_for_repo_strips_git_and_sanitizes():
    name = graph_name_for_repo("https://github.com/you/Acme.App.git")
    assert name.startswith("Acme_App-")  # ".git" stripped, "." sanitized
    assert name == graph_name_for_repo("https://github.com/you/Acme.App.git")  # deterministic
