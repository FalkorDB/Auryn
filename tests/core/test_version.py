"""`auryn --version` (core/version.py): the installed version plus the git commit it was built from,
parsed out of the git-derived version string (hatch-vcs)."""

from __future__ import annotations

import tomllib
from pathlib import Path

from click.testing import CliRunner

from auryn.core import version


def test_parse_a_build_of_a_modified_tree():
    assert version.parse("0.1.1.dev2+g0340f62a5.d20261116141317") == {
        "commit": "0340f62a5", "modified": True, "built": "2026-11-16 14:13:17 UTC",
    }


def test_parse_a_clean_commit_and_a_release():
    assert version.parse("0.3.1.dev4+gabc1234") == {"commit": "abc1234", "modified": False, "built": None}
    # a release tag, or the no-git fallback: no commit to report
    assert version.parse("0.3.0") == {"commit": None, "modified": False, "built": None}
    assert version.parse("0.1.0")["commit"] is None


def test_describe_names_the_version_and_the_commit(monkeypatch):
    monkeypatch.setattr(version, "_dist_version", {"auryn-core": "0.3.1.dev4+gabc1234"}.get)
    assert version.describe() == "auryn 0.3.1.dev4+gabc1234 (commit abc1234)"


def test_describe_says_when_a_modified_tree_was_built(monkeypatch):
    monkeypatch.setattr(version, "_dist_version", {"auryn-core": "0.1.1.dev2+g0340f62a5.d20261116141317"}.get)
    assert version.describe() == (
        "auryn 0.1.1.dev2+g0340f62a5.d20261116141317 "
        "(commit 0340f62a5; built from a modified tree at 2026-11-16 14:13:17 UTC)"
    )


def test_describe_without_git_metadata_is_the_version_only(monkeypatch):
    monkeypatch.setattr(version, "_dist_version", {"auryn-core": "0.3.0"}.get)
    assert version.describe() == "auryn 0.3.0"


def test_the_mcp_server_reports_the_installed_version(monkeypatch):
    """It announced a hard-coded `0.2.0` to every client, whatever was installed."""
    from types import SimpleNamespace

    from auryn.mcp import server

    monkeypatch.setattr(server, "installed", lambda: "0.8.2.dev4+gabc1234")
    mcp = server.create_server(SimpleNamespace(select_graph=lambda name: None), "g")
    assert mcp.version == "0.8.2.dev4+gabc1234"


def test_cli_version_flag_prints_and_exits():
    """Eager: no subcommand is needed (or run), and the line is exactly `describe()`."""
    from auryn.cli import cli

    result = CliRunner().invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert result.output == version.describe() + "\n"
    assert result.output.startswith("auryn ")


def test_every_package_takes_its_version_from_git():
    """The release builds every package at the tag's version through hatch-vcs -- nothing stamps a
    `version = "..."` line any more. A package that still carries one (a new plugin copied from an
    old template) would be published at that fixed version on every release."""
    root = Path(__file__).resolve().parents[2]
    files = [root / "pyproject.toml", *sorted((root / "packages").glob("*/pyproject.toml"))]
    assert len(files) > 20
    for f in files:
        data = tomllib.loads(f.read_text())
        assert "version" not in data["project"] and "version" in data["project"]["dynamic"], f
        assert "hatch-vcs" in data["build-system"]["requires"], f
        assert data["tool"]["hatch"]["version"]["source"] == "vcs", f


def test_an_archive_with_no_tag_behind_it_falls_back():
    """.git_archival.txt records the nearest tag only. With a `node:` line next to it, an archive
    of a commit no tag reaches (a fork, an export without tags) was built as `0.0` -- a release
    by its looks -- where a checkout of that commit is `0.0.0.dev<N>+g<sha>`. Without the line
    there is nothing to read, and the build falls back to 0.0.0.dev0 like any tree without git."""
    root = Path(__file__).resolve().parents[2]
    fields = [line.partition(":")[0] for line in (root / ".git_archival.txt").read_text().splitlines()]
    assert "describe-name" in fields and "node" not in fields
