"""`auryn embed` model selection: an explicit --model wins, else the AURYN_EMBED_MODEL
environment override is honored (matching the auto-embed path), else None lets get_embedder
pick its default. Pure CLI wiring -- no database, no real model download."""

from __future__ import annotations

from click.testing import CliRunner

import auryn.cli as cli_module
from auryn import cli


class _Stats:
    embedded = 0
    scanned = 0
    model_id = "stub"
    dim = 768
    index_ready = False


def _invoke_embed(monkeypatch, args):
    """Run `auryn embed` with every external touchpoint stubbed, capturing the model id the
    command hands to get_embedder."""
    captured: dict = {}
    monkeypatch.setattr(cli_module, "_resolve_connection", lambda *a, **k: object())

    class _Client:
        def select_graph(self, _name):
            return object()

    monkeypatch.setattr(cli_module, "connect_client", lambda _cfg: _Client())
    monkeypatch.setattr(cli_module, "GraphWriter", lambda g: g)

    import auryn.core.embeddings as emb

    def _fake_get_embedder(model_id=None, **_k):
        captured["model_id"] = model_id
        return object()

    monkeypatch.setattr(emb, "get_embedder", _fake_get_embedder)

    import auryn.core.embeddings.worker as worker
    monkeypatch.setattr(worker, "embed_graph", lambda _w, _e: _Stats())
    monkeypatch.setattr(worker, "reembed_all", lambda _w, _e: _Stats())

    result = CliRunner().invoke(cli.cli, ["embed", *args])
    return result, captured


def test_embed_honors_env_model_when_no_flag(monkeypatch):
    monkeypatch.setenv("AURYN_EMBED_MODEL", "jinaai/jina-embeddings-v2-small-en")
    result, captured = _invoke_embed(monkeypatch, [])
    assert result.exit_code == 0, result.output
    assert captured["model_id"] == "jinaai/jina-embeddings-v2-small-en"


def test_embed_flag_overrides_env_model(monkeypatch):
    monkeypatch.setenv("AURYN_EMBED_MODEL", "jinaai/jina-embeddings-v2-small-en")
    result, captured = _invoke_embed(monkeypatch, ["--model", "custom/model"])
    assert result.exit_code == 0, result.output
    assert captured["model_id"] == "custom/model"


def test_embed_without_flag_or_env_defers_to_default(monkeypatch):
    monkeypatch.delenv("AURYN_EMBED_MODEL", raising=False)
    result, captured = _invoke_embed(monkeypatch, [])
    assert result.exit_code == 0, result.output
    assert captured["model_id"] is None  # None -> get_embedder selects DEFAULT_LOCAL_MODEL
