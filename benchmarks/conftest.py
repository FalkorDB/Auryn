"""Standalone fixtures for the benchmark suite.

Deliberately NOT importing from tests/conftest.py -- benchmarks/ must stay fully
independent of the test tree, since it must never be picked up by a plain
`uv run pytest` (see pyproject.toml's `testpaths` and benchmarks/README.md). The
FalkorDB fixture below is an intentional, small duplicate of tests/conftest.py's.
"""

from __future__ import annotations

import os
import socket

import pytest
from falkordb import FalkorDB

from .config import BenchmarkConfig


def pytest_configure(config):
    """Force the benchmark suite to run SERIALLY, overriding the repo-wide `addopts = -n auto`
    (pyproject.toml). Two independent reasons the benchmarks must never parallelize:

      * pytest-benchmark silently disables all timing when xdist is active ("Benchmarks cannot be
        performed reliably in a parallelized environment") -- a benchmark run under `-n auto` would
        collect numbers for nothing.
      * the cases are order-dependent by design: `test_cold_ingest` populates the session-scoped
        graph + parse cache that `test_noop_resync`/`test_incremental_resync` reuse (see
        benchmark_cache_root / benchmark_graph below), so splitting them across workers fails.

    This conftest is an *initial* conftest whenever `pytest benchmarks/...` runs, so clearing the
    xdist options here (before workers spawn) reliably pins the benchmark session to one process --
    `pytest benchmarks/` just works, no `-n0` to remember. It only touches a benchmark invocation;
    the main `tests/` suite never loads this file and stays parallel."""
    if getattr(config.option, "numprocesses", None):
        config.option.numprocesses = 0
        config.option.dist = "no"


def _local_falkordb_available(host: str = "localhost", port: int = 6379) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def falkordb_connection():
    if _local_falkordb_available():
        yield FalkorDB(host="localhost", port=6379)
        return

    from testcontainers.core.container import DockerContainer
    from testcontainers.core.wait_strategies import LogMessageWaitStrategy

    container = DockerContainer("falkordb/falkordb:latest").with_exposed_ports(6379)
    container.waiting_for(LogMessageWaitStrategy(r"Ready to accept connections"))
    with container:
        host = container.get_container_host_ip()
        port = int(container.get_exposed_port(6379))
        yield FalkorDB(host=host, port=port)


@pytest.fixture(scope="session")
def benchmark_config() -> BenchmarkConfig:
    return BenchmarkConfig.from_env()


@pytest.fixture(scope="session")
def benchmark_cache_root(tmp_path_factory):
    """Fresh every whole `pytest benchmarks/` invocation -- true "cold" each run --
    but shared across the cold/no-op/incremental cases *within* one run, so the
    no-op and incremental cases actually reuse the cold case's cloned repo and
    warm parse cache instead of starting over."""
    return tmp_path_factory.mktemp("auryn_benchmark_cache")


@pytest.fixture(scope="session")
def benchmark_graph(falkordb_connection, benchmark_config):
    graph = falkordb_connection.select_graph(f"benchmark_{benchmark_config.project_name.lower()}")
    yield graph
    if not os.environ.get("BENCHMARK_KEEP_GRAPH"):
        graph.delete()
