"""The background deepen daemon: its producer-side queue (enqueue + coalesce), the per-request
drain/claim/retry/drop logic, idle self-termination, and single-flight via the run-lock. No real
subprocess is spawned -- `ensure_deepen_daemon` is stubbed and `ingest_directory` is faked. The one
thing the daemon does differently from the embed daemon: it re-runs the ingest with deep="inline"."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass

import pytest

import auryn.core.deepen.daemon as daemon
import auryn.core.ingest as ingest_mod
from auryn.core.home import deepen_lock_path, deepen_pid_path, deepen_requests_dir

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    """Point every ~/.auryn path at a tmp dir and never let a test spawn a real daemon."""
    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(daemon, "ensure_deepen_daemon", lambda: True)
    return tmp_path


@dataclass
class _Lang:
    deep_status: str
    language: str = "python"  # _deepen reads r.language when building its error list


@dataclass
class _Report:
    languages: list


class _FakeResult:
    def __init__(self, rows):
        self.result_set = rows


class _FakeClient:
    def __init__(self, project_exists=True):
        self._exists = project_exists  # what the project-presence guard in _deepen sees

    def select_graph(self, name):
        exists = self._exists

        class _G:
            _name = name

            def ro_query(self, cypher, params=None):  # the daemon's "is this project here?" check
                return _FakeResult([[1]] if exists else [])

        return _G()


# -- producer side ----------------------------------------------------------------------------
def test_request_deepen_enqueues_and_coalesces(tmp_path):
    daemon.request_deepen("graph-a", str(tmp_path), "proj")
    daemon.request_deepen("graph-a", str(tmp_path), "proj")  # same graph -> one queue entry
    files = list(deepen_requests_dir().glob("*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text()) == {"graph": "graph-a", "local_dir": str(tmp_path), "project_name": "proj"}


def test_request_path_distinct_per_graph():
    assert daemon._request_path("graph-a") != daemon._request_path("graph-b")


def test_distinct_graphs_make_distinct_queue_entries(tmp_path):
    daemon.request_deepen("graph-a", str(tmp_path))
    daemon.request_deepen("graph-b", str(tmp_path))
    assert len(list(deepen_requests_dir().glob("*.json"))) == 2


# -- liveness ---------------------------------------------------------------------------------
def test_is_daemon_running_false_without_pidfile():
    assert daemon.is_daemon_running() is False


@pytest.mark.skipif(fcntl is None, reason="os.kill liveness check is POSIX-only")
def test_is_daemon_running_reaps_stale_pid():
    deepen_pid_path().parent.mkdir(parents=True, exist_ok=True)
    deepen_pid_path().write_text("999999999")  # a pid that isn't alive
    assert daemon.is_daemon_running() is False


# -- drain: deepen one request ----------------------------------------------------------------
def _enqueue_raw(graph: str, local_dir: str, project_name: str = "proj"):
    deepen_requests_dir().mkdir(parents=True, exist_ok=True)
    path = daemon._request_path(graph)
    path.write_text(json.dumps({"graph": graph, "local_dir": local_dir, "project_name": project_name}))
    return path


def test_process_request_reingests_inline_and_clears_claim(monkeypatch, tmp_path):
    calls = []

    def _fake_ingest(writer, path, *, project_name=None, deep="inline", **kw):
        calls.append({"path": path, "project_name": project_name, "deep": deep})
        return _Report(languages=[_Lang("inline")])

    monkeypatch.setattr(ingest_mod, "ingest_directory", _fake_ingest)
    req = _enqueue_raw("g1", str(tmp_path))
    client = _FakeClient()
    out = daemon._process_request(req, client=client, attempts={})

    assert len(calls) == 1
    assert calls[0]["deep"] == "inline" and calls[0]["path"] == str(tmp_path)  # re-ingest with deep=inline
    assert out is client
    assert not req.exists() and not req.with_suffix(".inflight").exists()  # claim cleared on success


def test_process_request_skips_when_local_dir_gone(monkeypatch, tmp_path):
    spy = []
    monkeypatch.setattr(ingest_mod, "ingest_directory", lambda *a, **k: spy.append(1))
    req = _enqueue_raw("g-gone", str(tmp_path / "does-not-exist"))
    daemon._process_request(req, client=_FakeClient(), attempts={})
    assert spy == []                     # never tried to ingest a missing dir
    assert not req.exists()              # claim cleared (not retried -- it won't come back)


def test_process_request_retries_then_drops(monkeypatch, tmp_path):
    monkeypatch.setattr(ingest_mod, "ingest_directory",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("deep engine down")))
    monkeypatch.setattr(daemon, "_connect", _FakeClient)
    req = _enqueue_raw("g-flaky", str(tmp_path))
    key = req.name  # retry accounting is per (graph, project) request, not per graph name
    attempts: dict = {}
    for n in range(1, daemon._MAX_ATTEMPTS):
        daemon._process_request(req, client=None, attempts=attempts)
        assert attempts[key] == n and req.exists()  # re-queued
    daemon._process_request(req, client=None, attempts=attempts)
    assert not req.exists() and key not in attempts  # dropped after _MAX_ATTEMPTS


@pytest.mark.parametrize("payload", ["not json{", "[]", '{"graph": 1}', '{"graph": "g"}', '{"local_dir": "/x"}'])
def test_process_request_drops_malformed(monkeypatch, payload):
    spy = []
    monkeypatch.setattr(ingest_mod, "ingest_directory", lambda *a, **k: spy.append(1))
    deepen_requests_dir().mkdir(parents=True, exist_ok=True)
    req = daemon._request_path("bad")
    req.write_text(payload)
    daemon._process_request(req, client=_FakeClient(), attempts={})
    assert spy == []
    assert not req.exists() and not req.with_suffix(".inflight").exists()


def test_recover_inflight_requeues_crashed_claim(tmp_path):
    deepen_requests_dir().mkdir(parents=True, exist_ok=True)
    req = daemon._request_path("g-crashed")
    inflight = req.with_suffix(".inflight")
    inflight.write_text(json.dumps({"graph": "g-crashed", "local_dir": str(tmp_path)}))
    daemon._recover_inflight()
    assert req.exists() and not inflight.exists()


# -- idle self-termination + stop flag --------------------------------------------------------
def test_drain_loop_idle_exits(monkeypatch):
    monkeypatch.setattr(daemon, "_IDLE_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(daemon, "_POLL_SECONDS", 0.01)
    done = threading.Event()
    threading.Thread(target=lambda: (daemon._drain_loop({"flag": False}), done.set()), daemon=True).start()
    assert done.wait(timeout=3.0), "drain loop did not idle-exit"


def test_drain_loop_stops_on_flag(monkeypatch):
    monkeypatch.setattr(daemon, "_IDLE_GRACE_SECONDS", 999.0)
    monkeypatch.setattr(daemon, "_POLL_SECONDS", 0.01)
    stop = {"flag": False}
    done = threading.Event()
    threading.Thread(target=lambda: (daemon._drain_loop(stop), done.set()), daemon=True).start()
    time.sleep(0.05)
    stop["flag"] = True
    assert done.wait(timeout=3.0), "drain loop ignored the stop flag"


# -- single-flight ----------------------------------------------------------------------------
@pytest.mark.skipif(fcntl is None, reason="POSIX fcntl required for the run-lock")
def test_run_deepen_daemon_is_single_flight():
    deepen_lock_path().parent.mkdir(parents=True, exist_ok=True)
    held = open(deepen_lock_path(), "w")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        daemon.run_deepen_daemon()  # lock is taken -> bail immediately, no pidfile
        assert not deepen_pid_path().exists()
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()


def test_repos_sharing_a_graph_keep_separate_requests(tmp_path):
    # MCP can index several repos into ONE graph; each must keep its own pending deep request so one
    # doesn't overwrite another's (coalescing is per (graph, project), not per graph).
    daemon.request_deepen("shared", str(tmp_path / "a"), "proj-a")
    daemon.request_deepen("shared", str(tmp_path / "b"), "proj-b")
    daemon.request_deepen("shared", str(tmp_path / "a"), "proj-a")  # same repo again -> coalesces
    files = list(deepen_requests_dir().glob("*.json"))
    assert len(files) == 2
    projs = {json.loads(f.read_text())["project_name"] for f in files}
    assert projs == {"proj-a", "proj-b"}


def test_process_request_retries_on_reported_deep_error(monkeypatch, tmp_path):
    # ingest_directory returns NORMALLY with deep_status="error" (it records errors, doesn't raise);
    # the daemon must treat that as a failure so the request retries, not silently drop it as success.
    monkeypatch.setattr(ingest_mod, "ingest_directory", lambda *a, **k: _Report(languages=[_Lang("error")]))
    monkeypatch.setattr(daemon, "_connect", _FakeClient)
    req = _enqueue_raw("g-deeperr", str(tmp_path))
    daemon._process_request(req, client=None, attempts={})
    assert req.exists() and not req.with_suffix(".inflight").exists()  # re-queued, not dropped


def test_process_request_skips_when_project_absent_on_backend(monkeypatch, tmp_path):
    # The daemon connects to the CONFIGURED backend; if the project isn't there (a library caller used a
    # different server backend, or the graph was deleted), it must NOT full-ingest a graph there -- no-op.
    spy = []
    monkeypatch.setattr(ingest_mod, "ingest_directory", lambda *a, **k: spy.append(1))
    req = _enqueue_raw("g-absent", str(tmp_path))
    daemon._process_request(req, client=_FakeClient(project_exists=False), attempts={})
    assert spy == []                     # never full-ingested on the wrong backend
    assert not req.exists()              # claim cleared (not a retryable failure)


def test_retry_does_not_clobber_a_newer_request(monkeypatch, tmp_path):
    # While A runs, a producer publishes a NEWER request B (replacement checkout) at the same path. If A
    # then fails, its re-queue must NOT overwrite B with the stale A.
    req = _enqueue_raw("g-newer", str(tmp_path), "A")

    def _ingest_then_fail(*a, **k):
        req.write_text(json.dumps({"graph": "g-newer", "local_dir": str(tmp_path), "project_name": "B"}))  # B
        raise RuntimeError("A failed mid-run")

    monkeypatch.setattr(ingest_mod, "ingest_directory", _ingest_then_fail)
    monkeypatch.setattr(daemon, "_connect", _FakeClient)
    daemon._process_request(req, client=None, attempts={})

    assert req.exists() and json.loads(req.read_text())["project_name"] == "B"  # B preserved
    assert not req.with_suffix(".inflight").exists()                            # stale A dropped
