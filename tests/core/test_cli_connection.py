"""`auryn run` / `auryn git-sync` reach FalkorDB the way every other command does: the configured
backend unless `--host` says otherwise. They used to default to `localhost:6379`, whatever
`auryn install` had set up. `connect` is stubbed here -- no FalkorDB is contacted or started."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from auryn.cli import cli
from auryn.core.graph_client import FalkorDBConfig

# One real subcommand per group, with the arguments it requires.
_SUBCOMMAND = {
    "run": ["sql", "--dsn", "sqlite:///nowhere.db", "--database-name", "d"],
    "git-sync": ["csharp", "--repo-uri", "file:///nowhere", "--project-name", "p"],
}


class _Connected(Exception):
    """Raised by the stubbed `connect`: carries the config the command tried to connect with."""


@pytest.fixture
def connect_stub(monkeypatch):
    def connect(config):
        raise _Connected(config)

    monkeypatch.setattr("auryn.cli.connect", connect)  # git-sync's subcommands
    monkeypatch.setattr("auryn.ingestors.sql.ingestor.connect", connect)  # `run sql`


@pytest.mark.parametrize("group", ["run", "git-sync"])
def test_no_host_resolves_the_configured_backend(monkeypatch, connect_stub, group):
    configured = FalkorDBConfig(host="configured", port=7001)
    monkeypatch.setattr("auryn.cli._resolve_connection", lambda *conn: configured if conn[0] is None else None)

    res = CliRunner().invoke(cli, [group, "--graph-name", "g", *_SUBCOMMAND[group]])

    assert isinstance(res.exception, _Connected), res.output
    config = res.exception.args[0]
    assert (config.host, config.port, config.graph_name) == ("configured", 7001, "g")


@pytest.mark.parametrize("group", ["run", "git-sync"])
def test_an_explicit_host_connects_directly_there(connect_stub, group):
    res = CliRunner().invoke(cli, [group, "--host", "db.internal", "--port", "6400", *_SUBCOMMAND[group]])

    assert isinstance(res.exception, _Connected), res.output
    config = res.exception.args[0]
    assert (config.host, config.port, config.graph_name) == ("db.internal", 6400, "auryn")


@pytest.mark.parametrize("group", ["run", "git-sync"])
def test_help_resolves_nothing(monkeypatch, group):
    """click runs the group callback even for `--help` on a subcommand: it must not resolve the
    backend there -- for the local one that means starting its daemon."""

    def no_resolution(*conn):  # pragma: no cover - must not be called
        raise AssertionError("`--help` must not resolve a connection")

    monkeypatch.setattr("auryn.cli._resolve_connection", no_resolution)

    res = CliRunner().invoke(cli, [group, _SUBCOMMAND[group][0], "--help"])

    assert res.exit_code == 0, res.output


@pytest.mark.parametrize("group", ["run", "git-sync"])
@pytest.mark.parametrize("option", [["--port", "6380"], ["--password", "pw"], ["--ssl"]])
def test_a_connection_option_without_host_is_refused(monkeypatch, connect_stub, group, option):
    """`auryn run --port 6380 ...` used to mean localhost:6380. With no `--host` the configured
    backend is used now, so honouring nothing and saying nothing would write to the wrong store:
    it is a usage error, raised before any backend is resolved."""

    def no_resolution(*conn):  # pragma: no cover - must not be called
        raise AssertionError("a refused command must not resolve a connection")

    monkeypatch.setattr("auryn.cli._resolve_connection", no_resolution)

    res = CliRunner().invoke(cli, [group, *option, *_SUBCOMMAND[group]])

    assert res.exit_code == 2, res.output
    assert "need --host" in res.output


def test_the_lazy_connection_is_a_mapping_that_resolves_only_on_read(monkeypatch):
    """Ingestor plugins get `ctx.obj` and may treat it as the dict it used to be: `in` and `.get`
    work, and neither a membership test nor a missing key resolves (or starts) the backend."""
    from auryn.cli import _LazyConnection

    resolved = []
    monkeypatch.setattr("auryn.cli._resolve_connection", lambda *conn: resolved.append(conn) or FalkorDBConfig(host="h"))
    obj = _LazyConnection(None, None, None, None, False, "g")

    assert "falkordb_config" in obj and "other" not in obj
    assert (list(obj), len(obj), obj.get("other")) == (["falkordb_config"], 1, None)
    assert resolved == []

    assert (obj.get("falkordb_config").host, obj["falkordb_config"].graph_name) == ("h", "g")
    assert len(resolved) == 1  # resolved once, then kept
