"""The post-ingest embedding hook: off unless opted in (AURYN_EMBED), and -- when on -- routed by
the resolved embed mode. Background (the default) hands the graph to the detached embed daemon so
the ingest call isn't blocked on vector compute; inline embeds synchronously; off skips. A deep
pass still in flight defers (inline) or waits-then-enqueues (background). Never fails the ingest."""

from __future__ import annotations

import time

import auryn.core.ingest as ingest_mod
import auryn.core.embeddings.worker as worker_mod
import auryn.core.embeddings.daemon as daemon_mod
from auryn.core.embeddings.embedder import embeddings_enabled
from auryn.core.embeddings.worker import EmbedStats


class _FakeGraph:
    def __init__(self, name: str) -> None:
        self._name = name


class _FakeWriter:
    """Just enough surface for `_maybe_embed` to read the graph name off (`writer._graph._name`)."""

    def __init__(self, name: str = "repo_graph") -> None:
        self._graph = _FakeGraph(name)


def test_embeddings_enabled_defaults_on_with_escape_hatch(tmp_path, monkeypatch):
    """Embeddings are ON by default (built in the background, so they don't block a sync). AURYN_EMBED
    is only a low-level escape hatch to turn them off; `explicit` overrides everything."""
    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("AURYN_EMBED", raising=False)
    assert embeddings_enabled() is True                       # default: ON (no opt-in)
    for on in ("1", "true", "YES", "on"):
        monkeypatch.setenv("AURYN_EMBED", on)
        assert embeddings_enabled() is True
    for off in ("0", "false", "no", "off"):
        monkeypatch.setenv("AURYN_EMBED", off)
        assert embeddings_enabled() is False                  # the escape hatch
    assert embeddings_enabled(explicit=True) is True          # explicit wins over env
    assert embeddings_enabled(explicit=False) is False


def _spies(monkeypatch):
    """Spy on both routes: inline (embed_graph) and background (daemon.request_embed)."""
    inline: list = []
    enqueued: list = []
    monkeypatch.setattr(
        worker_mod, "embed_graph",
        lambda *a, **k: inline.append((a, k)) or EmbedStats(scanned=0, embedded=0, model_id="x", dim=3),
    )
    monkeypatch.setattr(daemon_mod, "request_embed", lambda graph, model=None: enqueued.append(graph))
    # The background route is gated on daemon_supported() (POSIX fcntl); force it on so these tests
    # exercise the routing logic on every platform. On native Windows it is otherwise False, which
    # correctly routes background -> inline (the fallback is covered by the inline/no-graph tests).
    monkeypatch.setattr(daemon_mod, "daemon_supported", lambda: True)
    return inline, enqueued


def test_hook_noop_when_disabled(monkeypatch):
    monkeypatch.setenv("AURYN_EMBED", "0")  # the escape hatch turns the whole pass off
    monkeypatch.delenv("AURYN_EMBED_MODE", raising=False)
    inline, enqueued = _spies(monkeypatch)
    ingest_mod._maybe_embed(_FakeWriter(), "inline")
    assert inline == [] and enqueued == []


def test_inline_mode_embeds_synchronously(monkeypatch):
    monkeypatch.setenv("AURYN_EMBED", "1")
    monkeypatch.setenv("AURYN_EMBED_MODE", "inline")
    inline, enqueued = _spies(monkeypatch)
    writer = _FakeWriter()
    ingest_mod._maybe_embed(writer, "inline")
    assert len(inline) == 1 and inline[0][0][0] is writer  # embed_graph got the writer
    assert enqueued == []


def test_background_mode_enqueues_and_does_not_block(monkeypatch):
    monkeypatch.setenv("AURYN_EMBED", "1")
    monkeypatch.setenv("AURYN_EMBED_MODE", "background")
    inline, enqueued = _spies(monkeypatch)
    ingest_mod._maybe_embed(_FakeWriter("my_graph"), "inline")
    assert enqueued == ["my_graph"]  # handed to the daemon, keyed by graph name
    assert inline == []              # no synchronous compute on the ingest path


def test_default_routes_to_background(tmp_path, monkeypatch):
    """The DEFAULT (nothing set) goes through _maybe_embed to the background daemon -- not just
    `embeddings_enabled()` returning True. Exercised with AURYN_EMBED unset and no config."""
    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "home"))  # empty config -> pure defaults
    monkeypatch.delenv("AURYN_EMBED", raising=False)          # override conftest's AURYN_EMBED=0
    monkeypatch.delenv("AURYN_EMBED_MODE", raising=False)
    inline, enqueued = _spies(monkeypatch)
    ingest_mod._maybe_embed(_FakeWriter("def_graph"), "inline")
    assert enqueued == ["def_graph"]  # background is the default route
    assert inline == []


def test_off_mode_skips_everything(monkeypatch):
    monkeypatch.setenv("AURYN_EMBED", "1")
    monkeypatch.setenv("AURYN_EMBED_MODE", "off")
    inline, enqueued = _spies(monkeypatch)
    ingest_mod._maybe_embed(_FakeWriter(), "inline")
    assert inline == [] and enqueued == []


def test_background_without_graph_name_falls_back_to_inline(monkeypatch):
    """A writer we can't read a graph name from can't be handed to the daemon (it reconnects by
    name), so background degrades to an inline embed rather than silently dropping it."""
    monkeypatch.setenv("AURYN_EMBED", "1")
    monkeypatch.setenv("AURYN_EMBED_MODE", "background")
    inline, enqueued = _spies(monkeypatch)
    ingest_mod._maybe_embed(object(), "inline")  # bare object: no _graph._name
    assert len(inline) == 1 and enqueued == []


def test_inline_defers_while_deep_background(monkeypatch):
    """Inline embedding can't run while a background deep pass is still mutating nodes (it would
    leave stale vectors the worker won't re-scan), so it defers."""
    monkeypatch.setenv("AURYN_EMBED", "1")
    monkeypatch.setenv("AURYN_EMBED_MODE", "inline")
    inline, enqueued = _spies(monkeypatch)
    ingest_mod._maybe_embed(_FakeWriter(), "background")
    assert inline == [] and enqueued == []


def test_background_deep_enqueues_after_deep_settles(monkeypatch):
    """background + deep=background: the enqueue waits for the deep pass (on a daemon thread, so
    the caller returns now), which also means deep=background ingests finally auto-embed."""
    monkeypatch.setenv("AURYN_EMBED", "1")
    monkeypatch.setenv("AURYN_EMBED_MODE", "background")
    inline, enqueued = _spies(monkeypatch)
    joined: list = []
    monkeypatch.setattr(ingest_mod, "join_deep_phase", lambda *a, **k: joined.append(True))

    ingest_mod._maybe_embed(_FakeWriter("deep_graph"), "background")
    deadline = time.time() + 2.0
    while time.time() < deadline and not enqueued:
        time.sleep(0.01)
    assert joined == [True]          # waited for deep first
    assert enqueued == ["deep_graph"]  # then enqueued
    assert inline == []


def test_hook_swallows_embed_failure(monkeypatch):
    monkeypatch.setenv("AURYN_EMBED", "1")
    monkeypatch.setenv("AURYN_EMBED_MODE", "inline")

    def _boom(*a, **k):
        raise RuntimeError("model download failed")

    monkeypatch.setattr(worker_mod, "embed_graph", _boom)
    ingest_mod._maybe_embed(_FakeWriter(), "inline")  # must not raise -- the ingest already succeeded
