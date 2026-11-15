"""The opt-in post-ingest embedding hook: off by default, runs when AURYN_EMBED is set,
skipped while a background deep pass is still in flight, and never fails the ingest."""

from __future__ import annotations

import auryn.core.ingest as ingest_mod
import auryn.core.embeddings.worker as worker_mod
from auryn.core.embeddings.embedder import embeddings_enabled
from auryn.core.embeddings.worker import EmbedStats


def test_embeddings_enabled_parses_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "home"))  # no config file -> config fallback is off
    monkeypatch.delenv("AURYN_EMBED", raising=False)
    assert embeddings_enabled() is False
    for on in ("1", "true", "YES", "on"):
        monkeypatch.setenv("AURYN_EMBED", on)
        assert embeddings_enabled() is True
    monkeypatch.setenv("AURYN_EMBED", "0")
    assert embeddings_enabled() is False
    assert embeddings_enabled(explicit=True) is True  # explicit overrides env


def test_embeddings_enabled_falls_back_to_config(tmp_path, monkeypatch):
    """With no AURYN_EMBED env var, the durable install setting (~/.auryn/config.toml) decides --
    the non-fragile path that survives shells/venvs/MCP restarts."""
    from auryn.core.app_config import set_auto_embed

    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("AURYN_EMBED", raising=False)

    assert embeddings_enabled() is False          # default: off
    set_auto_embed(True)
    assert embeddings_enabled() is True            # persisted setting turns it on
    set_auto_embed(False)
    assert embeddings_enabled() is False

    # An explicit env var overrides the persisted setting (per-run override).
    set_auto_embed(True)
    monkeypatch.setenv("AURYN_EMBED", "0")
    assert embeddings_enabled() is False
    monkeypatch.setenv("AURYN_EMBED", "1")
    assert embeddings_enabled() is True


def _spy(monkeypatch):
    calls = []
    monkeypatch.setattr(
        worker_mod, "embed_graph",
        lambda *a, **k: calls.append((a, k)) or EmbedStats(scanned=0, embedded=0, model_id="x", dim=3),
    )
    return calls


def test_hook_noop_when_disabled(monkeypatch):
    monkeypatch.delenv("AURYN_EMBED", raising=False)
    calls = _spy(monkeypatch)
    ingest_mod._maybe_embed(object(), "inline")
    assert calls == []


def test_hook_runs_when_enabled(monkeypatch):
    monkeypatch.setenv("AURYN_EMBED", "1")
    calls = _spy(monkeypatch)
    sentinel = object()
    ingest_mod._maybe_embed(sentinel, "inline")
    assert len(calls) == 1
    assert calls[0][0][0] is sentinel  # embed_graph called with the writer


def test_hook_skips_background_deep(monkeypatch):
    monkeypatch.setenv("AURYN_EMBED", "1")
    calls = _spy(monkeypatch)
    ingest_mod._maybe_embed(object(), "background")  # deep still running -> defer
    assert calls == []


def test_hook_swallows_embed_failure(monkeypatch):
    monkeypatch.setenv("AURYN_EMBED", "1")

    def _boom(*a, **k):
        raise RuntimeError("model download failed")

    monkeypatch.setattr(worker_mod, "embed_graph", _boom)
    # must not raise -- the ingest already succeeded
    ingest_mod._maybe_embed(object(), "inline")
