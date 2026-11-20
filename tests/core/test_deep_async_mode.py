"""The `deep="async"` orchestration in _run_deep_phase: enqueue to the detached deepen daemon (so the
deep pass survives a one-shot CLI exiting), and fall back to inline when there's nothing a daemon could
re-ingest (a remote-URI source with no local checkout)."""

from __future__ import annotations

import auryn.core.deepen.daemon as deepen_daemon
import auryn.core.ingest as ingest_mod


def _lang(name="python"):
    return type("L", (), {"language": name, "deep_sync": object(), "is_two_phase": lambda self: True})()


def _writer(graph_name="g1"):
    return type("W", (), {"_graph": type("G", (), {"_name": graph_name})()})()


def test_deep_async_enqueues_to_daemon(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(deepen_daemon, "request_deepen", lambda g, ld, pn: calls.append((g, ld, pn)))
    monkeypatch.setattr(deepen_daemon, "daemon_supported", lambda: True)
    src = type("S", (), {"_root": str(tmp_path)})()

    results, status = ingest_mod._run_deep_phase(
        _writer("g1"), src, "sha", [_lang("python")], "async",
        str(tmp_path), "proj", None, None, None,
    )
    assert status == {"python": "async"}          # queued, not run inline
    assert results == {}                          # no synchronous deep result
    assert calls == [("g1", str(tmp_path), "proj")]  # handed the repo identity to the daemon


def test_deep_async_falls_back_to_inline_without_local_dir(monkeypatch):
    # A remote-URI source with no local checkout: nothing the daemon could re-ingest -> run inline.
    monkeypatch.setattr(deepen_daemon, "daemon_supported", lambda: True)
    monkeypatch.setattr(deepen_daemon, "request_deepen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not enqueue")))
    ran = []

    def _fake_run_phase(writer, source, target_sha, lang, fn, phase, *ctx):
        ran.append(lang.language)
        return object(), None  # (result, error)

    monkeypatch.setattr(ingest_mod, "_run_phase", _fake_run_phase)
    src = type("S", (), {})()  # no _root

    results, status = ingest_mod._run_deep_phase(
        _writer("g1"), src, "sha", [_lang("python")], "async",
        "https://example.com/remote.git", "proj", None, None, None,  # source_uri is not a local dir
    )
    assert ran == ["python"] and status == {"python": "inline"}


def test_deep_async_noop_without_two_phase_languages(monkeypatch):
    # Nothing to deepen -> no daemon spawn, empty result (the common fast-only-repo case).
    monkeypatch.setattr(deepen_daemon, "request_deepen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not enqueue")))
    results, status = ingest_mod._run_deep_phase(_writer(), object(), "sha", [], "async",
                                                 "/x", "proj", None, None, None)
    assert results == {} and status == {}
