"""Baseline benchmark: cold-ingest a large, real public C# repository via
git_sync, then measure a no-op re-sync and an incremental re-sync between two
real, adjacent tagged releases. See benchmarks/README.md for the full story
(why git_sync, why this repo, how to point it at a different one).

Run:
    uv run pytest benchmarks/ --benchmark-only -s \
        --benchmark-json=benchmarks/results/latest.json

Not part of the normal `uv run pytest` test suite -- see pyproject.toml's
`testpaths` and benchmarks/README.md. Clones a real, large (~380MB by default)
public repository over the network and can take several minutes.

These three cases share one graph and one warm parse cache and must run in this
file's order (pytest runs a file top to bottom; no reordering plugin is active
here) -- each one's docstring says what state it depends on.
"""

from __future__ import annotations

from auryn.core.graph_client import GraphWriter
from auryn.ingestors.csharp.git_sync import GitCSharpProjectConfig, SyncStats, sync

from .config import BenchmarkConfig
from .report import print_summary


def _project_config(benchmark_config: BenchmarkConfig) -> GitCSharpProjectConfig:
    return GitCSharpProjectConfig(
        repo_uri=benchmark_config.repo_uri,
        project_name=benchmark_config.project_name,
        workers=benchmark_config.workers,
    )


def _record(benchmark, title: str, config: GitCSharpProjectConfig, ref: str, stats: SyncStats) -> None:
    rows: dict[str, object] = {
        "repo_uri": config.repo_uri,
        "ref": ref,
        "workers": config.workers or "os.cpu_count()",
        "elapsed_seconds": round(stats.elapsed_seconds, 3),
        "files_parsed": stats.files_parsed,
        "files_cached": stats.files_cached,
        "nodes_added": stats.nodes_added,
        "nodes_updated": stats.nodes_updated,
        "nodes_removed": stats.nodes_removed,
        "edges_added": stats.edges_added,
        "edges_updated": stats.edges_updated,
        "edges_removed": stats.edges_removed,
    }
    if stats.elapsed_seconds > 0:
        rows["nodes_added_per_sec"] = round(stats.nodes_added / stats.elapsed_seconds, 1)

    print_summary(title, rows)
    for key, value in rows.items():
        benchmark.extra_info[key] = value


def test_cold_ingest(benchmark, benchmark_config, benchmark_graph, benchmark_cache_root):
    """Fresh clone, fresh parse cache, full ingest at `cold_ref` from an empty
    graph -- the worst-case "first contact" number."""
    config = _project_config(benchmark_config)
    writer = GraphWriter(benchmark_graph)

    stats = benchmark.pedantic(
        lambda: sync(writer, config, ref=benchmark_config.cold_ref, cache_root=benchmark_cache_root),
        rounds=1,
        iterations=1,
    )

    _record(benchmark, "Cold ingest", config, benchmark_config.cold_ref, stats)
    assert stats.nodes_added > 0
    assert stats.files_cached == 0  # nothing could have been cached before this


def test_noop_resync(benchmark, benchmark_config, benchmark_graph, benchmark_cache_root):
    """Re-syncs to the *same* ref test_cold_ingest just landed on -- proves the
    fast-path (an unchanged commit costs one property lookup, not a re-parse).
    Depends on test_cold_ingest having already run in this same session."""
    config = _project_config(benchmark_config)
    writer = GraphWriter(benchmark_graph)

    stats = benchmark.pedantic(
        lambda: sync(writer, config, ref=benchmark_config.cold_ref, cache_root=benchmark_cache_root),
        rounds=1,
        iterations=1,
    )

    _record(benchmark, "No-op resync (same ref)", config, benchmark_config.cold_ref, stats)
    assert stats.nodes_added == 0
    assert stats.nodes_removed == 0
    assert stats.files_parsed == 0  # short-circuits before ever touching a file


def test_incremental_resync(benchmark, benchmark_config, benchmark_graph, benchmark_cache_root):
    """Moves the same graph from `cold_ref` to `warm_ref` (a newer real tag) with
    a warm parse cache already in place -- the number that actually demonstrates
    the point of git_sync's blob-sha parse cache: files_cached should dwarf
    files_parsed even though the whole repo is large. Depends on test_cold_ingest
    having already run in this same session."""
    config = _project_config(benchmark_config)
    writer = GraphWriter(benchmark_graph)

    stats = benchmark.pedantic(
        lambda: sync(writer, config, ref=benchmark_config.warm_ref, cache_root=benchmark_cache_root),
        rounds=1,
        iterations=1,
    )

    _record(benchmark, "Incremental resync (adjacent real release)", config, benchmark_config.warm_ref, stats)
    # A real run between these two adjacent tags: nodes_added=30, nodes_removed=8 --
    # a small fraction of test_cold_ingest's ~188k. 1000 gives headroom for normal
    # drift while still catching a regression back to a near-total replacement (a
    # real bug this benchmark caught once already: GraphWriter.read_fingerprints()
    # (then read_subgraph()) silently truncated at FalkorDB's default 10000-row
    # RESULTSET_SIZE cap, making ~178k already-existing nodes look "new").
    assert stats.nodes_added < 1000
    assert stats.nodes_removed < 1000
