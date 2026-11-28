"""Unified parse-pool sizing: `resolve_workers` (core.parallel) is the one place every pool-based
ingestor (Python, C#, PL/SQL, oraschema) gets its worker count, and `auryn ingest --workers N` sets
AURYN_PARSE_WORKERS so one knob caps parallelism across all languages a run touches."""

from __future__ import annotations

import os

import pytest

from auryn.core.parallel import resolve_workers


@pytest.fixture(autouse=True)
def _clean_env():
    # Save/restore directly (not via monkeypatch): the CLI test mutates os.environ itself, which
    # monkeypatch's teardown wouldn't undo -- so clean before AND after to isolate every test.
    prev = os.environ.pop("AURYN_PARSE_WORKERS", None)
    yield
    os.environ.pop("AURYN_PARSE_WORKERS", None)
    if prev is not None:
        os.environ["AURYN_PARSE_WORKERS"] = prev


def test_explicit_request_wins_over_env_and_cpu(monkeypatch):
    monkeypatch.setenv("AURYN_PARSE_WORKERS", "3")
    assert resolve_workers(7) == 7  # an explicit --workers / config.workers beats the env


def test_env_used_when_no_explicit_request(monkeypatch):
    monkeypatch.setenv("AURYN_PARSE_WORKERS", "2")
    assert resolve_workers(None) == 2
    assert resolve_workers(0) == 2  # 0/None are both "unspecified"


def test_falls_back_to_cpu_count_when_unset():
    assert resolve_workers(None) == (os.cpu_count() or 1)


@pytest.mark.parametrize("bad", ["bogus", "0", "-4", ""])
def test_invalid_env_falls_through_to_cpu(monkeypatch, bad):
    monkeypatch.setenv("AURYN_PARSE_WORKERS", bad)
    assert resolve_workers(None) == (os.cpu_count() or 1)


def test_ingest_dir_workers_flag_sets_env(monkeypatch, tmp_path):
    # `auryn ingest dir --workers N` must export AURYN_PARSE_WORKERS before ingestion so every pool sees it.
    import auryn.cli as cli
    from auryn.core import ingest as ingest_mod

    captured = {}

    def _fake_ingest(writer, path, **k):
        captured["env"] = os.environ.get("AURYN_PARSE_WORKERS")
        return type("R", (), {"languages": []})()

    monkeypatch.setattr(cli, "_ingest_writer", lambda ctx, graph: None)
    monkeypatch.setattr(cli, "_wire_trigger", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_echo_ingest_report", lambda report, background_failures=None: [])
    monkeypatch.setattr(ingest_mod, "ingest_directory", _fake_ingest)
    monkeypatch.setattr(ingest_mod, "join_deep_phase", lambda *a, **k: [])
    monkeypatch.delenv("AURYN_PARSE_WORKERS", raising=False)

    from click.testing import CliRunner
    res = CliRunner().invoke(cli.cli, ["ingest", "dir", str(tmp_path), "--workers", "2"])
    assert res.exit_code == 0, res.output
    assert captured["env"] == "2"  # the pool size was exported before ingest ran
