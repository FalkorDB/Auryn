"""#851: on a schema-incompatible Auryn upgrade, a LOCAL machine re-ingests its registered repos from
scratch (one-time), while a REMOTE/shared DB is never destroyed (only warned about). Detection is a
pure version comparison (Config.schema_version vs GRAPH_SCHEMA_VERSION) -- the graph is never scanned.

Pure-unit over monkeypatched load_config/set_schema_version + an injected local_migrate."""

from __future__ import annotations

import dataclasses

import pytest

from auryn.core import schema_migration
from auryn.core.app_config import Backend, Config, RepoRegistration


@pytest.fixture
def harness(monkeypatch):
    """Drives schema_migration with an in-memory config: records set_schema_version calls and whether
    local_migrate ran. `run(cfg)` returns (migrated, calls, bumped_to)."""
    state = {"cfg": None, "bumped": [], "migrated_repos": None}

    monkeypatch.setattr(schema_migration, "load_config", lambda: state["cfg"])
    monkeypatch.setattr(schema_migration, "set_schema_version", lambda v: state["bumped"].append(v))
    monkeypatch.setattr(schema_migration, "GRAPH_SCHEMA_VERSION", 2)  # pretend current is v2
    # reset the module-level re-entry guard between tests
    schema_migration._state = type(schema_migration._state)()

    def run(cfg):
        state["cfg"] = cfg

        def local_migrate(repos):
            state["migrated_repos"] = list(repos)

        migrated = schema_migration.migrate_schema_if_stale(is_local=cfg.backend.mode == "local",
                                                            local_migrate=local_migrate, current_version=2)
        return migrated, state

    return run


def _cfg(version, mode="local", repos=()):
    return Config(backend=Backend(mode=mode), schema_version=version, repos=repos)


_REPO = RepoRegistration(local_dir="/x/repoA", graph_name="repoA")


def test_up_to_date_is_a_noop(harness):
    migrated, st = harness(_cfg(version=2, repos=(_REPO,)))
    assert migrated is False
    assert st["bumped"] == [] and st["migrated_repos"] is None  # nothing touched


def test_local_stale_with_repos_reingests_and_bumps(harness):
    migrated, st = harness(_cfg(version=1, mode="local", repos=(_REPO,)))
    assert migrated is True
    assert st["migrated_repos"] == [_REPO]        # the destructive local rebuild ran...
    assert st["bumped"] == [2]                    # ...and the version was bumped afterward


def test_remote_stale_is_warned_not_destroyed(harness, caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="auryn.ingest"):
        migrated, st = harness(_cfg(version=0, mode="remote", repos=(_REPO,)))
    assert migrated is False
    assert st["migrated_repos"] is None           # a shared DB is NEVER auto-destroyed
    assert st["bumped"] == [2]                     # warned once, then bumped so it doesn't nag
    assert any("REMOTE/shared" in r.message or "remote" in r.message.lower() for r in caplog.records)


def test_local_stale_no_repos_just_adopts_version(harness):
    migrated, st = harness(_cfg(version=0, mode="local", repos=()))
    assert migrated is False
    assert st["migrated_repos"] is None           # nothing ingested here yet -> nothing to rebuild
    assert st["bumped"] == [2]                     # just adopt the current version


def test_reentry_guard_blocks_nested_migration(harness, monkeypatch):
    # While a migration is running, a nested call (e.g. the re-ingest it launches) must not recurse.
    schema_migration._state.active = True
    try:
        migrated, st = harness(_cfg(version=0, mode="local", repos=(_REPO,)))
    finally:
        schema_migration._state.active = False
    assert migrated is False and st["migrated_repos"] is None and st["bumped"] == []


def test_config_schema_version_round_trips(tmp_path, monkeypatch):
    # schema_version persists through save/load (so detection survives across process runs).
    from auryn.core import app_config
    monkeypatch.setattr(app_config, "config_path", lambda: tmp_path / "config.toml")
    app_config.save_config(dataclasses.replace(Config(), schema_version=3))
    assert app_config.load_config().schema_version == 3
    # absent key -> 0 (pre-#851 / fresh)
    (tmp_path / "config.toml").write_text('[backend]\nmode = "local"\n')
    assert app_config.load_config().schema_version == 0


def test_cli_local_migrate_deletes_graphs_and_reingests(monkeypatch, tmp_path):
    # The CLI closure: a stale LOCAL machine drops each registered graph and re-ingests each repo,
    # then the stored version is current. (connect/ingest are faked; no DB.)
    import auryn.cli as cli
    from auryn.core import app_config, ingest as ingest_mod
    from auryn.core.graph_model import GRAPH_SCHEMA_VERSION

    schema_migration._state = type(schema_migration._state)()  # clear re-entry guard
    repo_dir = tmp_path / "repoA"
    repo_dir.mkdir()
    monkeypatch.setattr(app_config, "config_path", lambda: tmp_path / "config.toml")
    app_config.save_config(Config(backend=Backend(mode="local"), schema_version=0,
                                  repos=(RepoRegistration(local_dir=str(repo_dir), graph_name="gA"),)))

    deleted, reingested = [], []

    class _FakeGraph:
        def __init__(self, name):
            self.name = name

        def delete(self):
            deleted.append(self.name)

    class _FakeClient:
        def select_graph(self, name):
            return _FakeGraph(name)

    monkeypatch.setattr(cli, "_resolve_connection", lambda *a, **k: object())
    monkeypatch.setattr(cli, "connect_client", lambda config: _FakeClient())
    monkeypatch.setattr(ingest_mod, "ingest_directory", lambda writer, path, **k: reingested.append(path))

    cli._migrate_schema_if_stale(None, None, None, None, False)

    assert deleted == ["gA"]
    assert reingested == [str(repo_dir)]
    assert app_config.load_config().schema_version == GRAPH_SCHEMA_VERSION


def test_cli_explicit_host_is_treated_as_remote_not_destroyed(monkeypatch, tmp_path):
    # Any explicit --host => remote/shared => never delete, even if the configured backend is local.
    import auryn.cli as cli
    from auryn.core import app_config, ingest as ingest_mod

    schema_migration._state = type(schema_migration._state)()
    monkeypatch.setattr(app_config, "config_path", lambda: tmp_path / "config.toml")
    app_config.save_config(Config(backend=Backend(mode="local"), schema_version=0,
                                  repos=(RepoRegistration(local_dir=str(tmp_path), graph_name="gA"),)))
    deleted = []
    monkeypatch.setattr(cli, "connect_client", lambda config: type("C", (), {"select_graph": lambda s, n: None})())
    monkeypatch.setattr(ingest_mod, "ingest_directory", lambda *a, **k: deleted.append("INGEST"))

    cli._migrate_schema_if_stale("remote.example.com", 6379, None, None, False)

    assert deleted == []  # a --host target is never auto-destroyed/re-ingested
