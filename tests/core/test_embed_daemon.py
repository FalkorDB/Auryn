"""The background embed daemon: its producer-side queue (enqueue + coalesce), the per-request
drain/claim/retry/drop logic, idle self-termination, and single-flight via the run-lock. No real
subprocess is spawned -- `ensure_embed_daemon` is stubbed and `embed_graph` is faked."""

from __future__ import annotations

import json
import threading
import time

import pytest

import auryn.core.embeddings.daemon as daemon
import auryn.core.embeddings.worker as worker_mod
from auryn.core.embeddings.worker import EmbedStats
from auryn.core.home import embed_lock_path, embed_pid_path, embed_requests_dir

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    """Point every ~/.auryn path at a tmp dir and never let a test spawn a real daemon."""
    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(daemon, "ensure_embed_daemon", lambda: True)
    return tmp_path


class _FakeClient:
    def select_graph(self, name):
        return daemon_graph_stub(name)


def daemon_graph_stub(name):
    class _G:
        _name = name
    return _G()


# ── producer side ───────────────────────────────────────────────────────────────────────────
def test_request_embed_enqueues_and_coalesces():
    daemon.request_embed("graph-a")
    daemon.request_embed("graph-a")  # same graph again -> must collapse to one queue entry
    files = list(embed_requests_dir().glob("*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text()) == {"graph": "graph-a"}


def test_request_path_distinct_per_graph():
    assert daemon._request_path("graph-a") != daemon._request_path("graph-b")


def test_distinct_graphs_make_distinct_queue_entries():
    daemon.request_embed("graph-a")
    daemon.request_embed("graph-b")
    assert len(list(embed_requests_dir().glob("*.json"))) == 2


# ── liveness ────────────────────────────────────────────────────────────────────────────────
def test_is_daemon_running_false_without_pidfile():
    assert daemon.is_daemon_running() is False


@pytest.mark.skipif(fcntl is None, reason="os.kill liveness check is POSIX-only (Windows raises OSError)")
def test_is_daemon_running_reaps_stale_pid():
    embed_pid_path().parent.mkdir(parents=True, exist_ok=True)
    embed_pid_path().write_text("999999999")  # a pid that isn't alive
    assert daemon.is_daemon_running() is False


# ── drain: embed one request ──────────────────────────────────────────────────────────────────
def _enqueue_raw(graph: str):
    """Drop a request file the way a producer would, without spawning anything."""
    embed_requests_dir().mkdir(parents=True, exist_ok=True)
    path = daemon._request_path(graph)
    path.write_text(json.dumps({"graph": graph}))
    return path


def test_process_request_embeds_and_clears_claim(monkeypatch):
    calls = []
    monkeypatch.setattr(
        worker_mod, "embed_graph",
        lambda writer, embedder: calls.append(writer) or EmbedStats(scanned=2, embedded=2, model_id="x", dim=3),
    )
    req = _enqueue_raw("g1")
    client = _FakeClient()
    out = daemon._process_request(req, embedder_for=lambda m: object(), client=client, attempts={})

    assert len(calls) == 1              # embed_graph ran for the graph
    assert out is client               # the client is cached + returned for reuse
    assert not req.exists()            # request consumed
    assert not req.with_suffix(".inflight").exists()  # claim cleared on success


def test_process_request_retries_then_drops(monkeypatch):
    monkeypatch.setattr(worker_mod, "embed_graph",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("DB down")))
    monkeypatch.setattr(daemon, "_connect", lambda: _FakeClient())
    req = _enqueue_raw("g-flaky")
    attempts: dict = {}

    # First _MAX_ATTEMPTS-1 failures re-queue the request for a later pass.
    for n in range(1, daemon._MAX_ATTEMPTS):
        daemon._process_request(req, embedder_for=lambda m: object(), client=None, attempts=attempts)
        assert attempts["g-flaky"] == n
        assert req.exists()                               # re-queued
        assert not req.with_suffix(".inflight").exists()

    # The final failure drops it (nodes stay pending; a future ingest re-requests them).
    daemon._process_request(req, embedder_for=lambda m: object(), client=None, attempts=attempts)
    assert not req.exists()
    assert not req.with_suffix(".inflight").exists()
    assert "g-flaky" not in attempts


def test_process_request_drops_malformed(monkeypatch):
    spy = []
    monkeypatch.setattr(worker_mod, "embed_graph", lambda *a, **k: spy.append(1))
    embed_requests_dir().mkdir(parents=True, exist_ok=True)
    req = daemon._request_path("bad")
    req.write_text("not json{")
    daemon._process_request(req, embedder_for=lambda m: object(), client=_FakeClient(), attempts={})
    assert spy == []                       # never tried to embed
    assert not req.exists() and not req.with_suffix(".inflight").exists()  # dropped


def test_recover_inflight_requeues_crashed_claim():
    """A request a prior daemon claimed (`<g>.inflight`) but crashed before finishing is renamed
    back to `<g>.json` so the drain loop (which scans only `*.json`) picks it up again."""
    embed_requests_dir().mkdir(parents=True, exist_ok=True)
    req = daemon._request_path("g-crashed")
    inflight = req.with_suffix(".inflight")
    inflight.write_text(json.dumps({"graph": "g-crashed"}))
    daemon._recover_inflight()
    assert req.exists() and not inflight.exists()
    assert json.loads(req.read_text())["graph"] == "g-crashed"


def test_request_embed_records_model():
    daemon.request_embed("g-model", model="jinaai/custom-code")
    files = list(embed_requests_dir().glob("*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text()) == {"graph": "g-model", "model": "jinaai/custom-code"}


def test_process_request_uses_request_model(monkeypatch):
    """The daemon embeds each graph with the model its request carried, not the daemon's ambient one."""
    seen = {}
    monkeypatch.setattr(worker_mod, "embed_graph",
                        lambda w, e: EmbedStats(scanned=1, embedded=1, model_id="x", dim=3))
    embed_requests_dir().mkdir(parents=True, exist_ok=True)
    req = daemon._request_path("g-m")
    req.write_text(json.dumps({"graph": "g-m", "model": "custom-model"}))

    def _embedder_for(m):
        seen["model"] = m
        return object()

    daemon._process_request(req, embedder_for=_embedder_for, client=_FakeClient(), attempts={})
    assert seen["model"] == "custom-model"


# ── idle self-termination ─────────────────────────────────────────────────────────────────────
def test_drain_loop_idle_exits(monkeypatch):
    monkeypatch.setattr(daemon, "_IDLE_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(daemon, "_POLL_SECONDS", 0.01)
    # get_embedder is constructed but never asked to embed (empty queue), so no model loads.
    done = threading.Event()

    def _run():
        daemon._drain_loop({"flag": False})
        done.set()

    threading.Thread(target=_run, daemon=True).start()
    assert done.wait(timeout=3.0), "drain loop did not idle-exit"


def test_drain_loop_stops_on_flag(monkeypatch):
    monkeypatch.setattr(daemon, "_IDLE_GRACE_SECONDS", 999.0)  # would never idle-exit
    monkeypatch.setattr(daemon, "_POLL_SECONDS", 0.01)
    stop = {"flag": False}
    done = threading.Event()
    threading.Thread(target=lambda: (daemon._drain_loop(stop), done.set()), daemon=True).start()
    time.sleep(0.05)
    stop["flag"] = True
    assert done.wait(timeout=3.0), "drain loop ignored the stop flag"


# ── single-flight ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.skipif(fcntl is None, reason="POSIX fcntl required for the run-lock")
def test_run_embed_daemon_is_single_flight():
    """A second daemon can't start while the run-lock is held: run_embed_daemon returns at once
    and never writes a pidfile."""
    embed_lock_path().parent.mkdir(parents=True, exist_ok=True)
    held = open(embed_lock_path(), "w")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        daemon.run_embed_daemon()  # lock is taken -> should bail immediately
        assert not embed_pid_path().exists()
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()


# ── end-to-end: real drain -> real embed_graph -> real FalkorDB vector index ───────────────────
class _FakeEmbedder:
    """Deterministic 3-dim space (no model download); shares test_embed_worker's scheme."""

    model_id = "fake-test-model"
    dim = 3

    def _vec(self, text):
        t = text.lower()
        return [1.0 if "connect" in t else 0.0, 1.0 if "widget" in t else 0.0, 0.1]

    def embed(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)


def test_drain_request_actually_embeds_and_indexes(tmp_path):
    """The full request-handling path against a real (embedded) FalkorDB: a queued graph gets its
    pending nodes embedded and the vector index built, and the claim file is cleared -- proving the
    daemon's drain does real work, not just bookkeeping."""
    pytest.importorskip("redislite.falkordb_client", reason="falkordblite (embedded extra) not installed")
    from auryn.core.graph_client import FalkorDBConfig, GraphWriter, connect_client

    client = connect_client(FalkorDBConfig(embedded=True, db_path=str(tmp_path / "e2e.db")))
    GraphWriter(client.select_graph("g-e2e")).query(
        "CREATE (:Function {id:'f1', name:'connect', qualified_name:'db.connect', "
        "doc:'Open a db connection.', source:'def connect(): ...'})"
    )
    req = _enqueue_raw("g-e2e")

    out = daemon._process_request(req, embedder_for=lambda m: _FakeEmbedder(), client=client, attempts={})

    assert out is client
    assert not req.exists() and not req.with_suffix(".inflight").exists()  # claim cleared
    # The node now carries a vector and the KNN index answers -- real embedding happened.
    hits = GraphWriter(client.select_graph("g-e2e")).knn_search(
        _FakeEmbedder().embed_query("open a connection"), 1, return_properties=("name",)
    )
    assert hits and hits[0]["name"] == "connect"
