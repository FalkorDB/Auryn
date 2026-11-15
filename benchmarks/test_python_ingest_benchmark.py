"""Ingestion benchmark for the Python engines against a large, real public repo.

Cold-ingests a large Python codebase via the Python git_sync, then a no-op re-sync. The
engine is env-selectable:
  * default "ast"  -- structural throughput at scale (fast, linear ~300 files/s);
  * "jedi"         -- the default deep engine (inferred types + resolved calls). Far
    slower per file (in-process static inference at every position), so prefer a smaller
    repo/subset via BENCHMARK_PY_REPO when measuring it.

Run:
    uv run pytest benchmarks/test_python_ingest_benchmark.py --benchmark-only -s \
        --benchmark-json=benchmarks/results/latest_python.json
    # deep engine on a smaller repo:
    BENCHMARK_PY_ENGINE=jedi BENCHMARK_PY_REPO=https://github.com/psf/requests.git \
        BENCHMARK_PY_COLD_REF=main uv run pytest benchmarks/test_python_ingest_benchmark.py -s

Clones a real repo over the network; not part of `uv run pytest` (see pyproject `testpaths`).
The two cases share one graph + cache and must run in file order.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from auryn.core.graph_client import GraphWriter
from auryn.ingestors.python.git_sync import GitPythonProjectConfig, SyncStats, sync

from .report import print_summary

_REPO = os.environ.get("BENCHMARK_PY_REPO", "https://github.com/django/django.git")
_NAME = os.environ.get("BENCHMARK_PY_PROJECT", "django")
_COLD_REF = os.environ.get("BENCHMARK_PY_COLD_REF", "5.1")
_ENGINE = os.environ.get("BENCHMARK_PY_ENGINE", "ast")


@pytest.fixture(scope="module")
def py_graph(falkordb_connection):
    graph = falkordb_connection.select_graph(f"benchmark_py_{uuid4().hex[:8]}")
    yield graph
    if not os.environ.get("BENCHMARK_KEEP_GRAPH"):
        graph.delete()


@pytest.fixture(scope="module")
def py_cache_root(tmp_path_factory):
    return tmp_path_factory.mktemp("py_benchmark_cache")


def _record(benchmark, title: str, ref: str, stats: SyncStats) -> None:
    rows: dict[str, object] = {
        "repo_uri": _REPO,
        "ref": ref,
        "engine": _ENGINE,
        "elapsed_seconds": round(stats.elapsed_seconds, 3),
        "files_parsed": stats.files_parsed,
        "nodes_added": stats.nodes_added,
        "edges_added": stats.edges_added,
        "nodes_removed": stats.nodes_removed,
        "edges_removed": stats.edges_removed,
    }
    if stats.elapsed_seconds > 0:
        rows["files_per_sec"] = round(stats.files_parsed / stats.elapsed_seconds, 1)
        rows["nodes_per_sec"] = round(stats.nodes_added / stats.elapsed_seconds, 1)
    print_summary(title, rows)
    for key, value in rows.items():
        benchmark.extra_info[key] = value


def test_cold_ingest(benchmark, py_graph, py_cache_root):
    """Fresh clone + fresh graph, full ingest at `cold_ref` -- the worst-case number."""
    config = GitPythonProjectConfig(repo_uri=_REPO, project_name=_NAME)
    stats = benchmark.pedantic(
        lambda: sync(GraphWriter(py_graph), config, ref=_COLD_REF, cache_root=py_cache_root, engine=_ENGINE),
        rounds=1,
        iterations=1,
    )
    _record(benchmark, f"Cold ingest ({_ENGINE})", _COLD_REF, stats)
    assert stats.nodes_added > 0


def test_noop_resync(benchmark, py_graph, py_cache_root):
    """Re-sync to the same ref -- the commit-sha short-circuit (one property lookup, no
    re-parse). Depends on test_cold_ingest having run in this session."""
    config = GitPythonProjectConfig(repo_uri=_REPO, project_name=_NAME)
    stats = benchmark.pedantic(
        lambda: sync(GraphWriter(py_graph), config, ref=_COLD_REF, cache_root=py_cache_root, engine=_ENGINE),
        rounds=1,
        iterations=1,
    )
    _record(benchmark, "No-op resync (same ref)", _COLD_REF, stats)
    assert stats.nodes_added == 0
    assert stats.files_parsed == 0
