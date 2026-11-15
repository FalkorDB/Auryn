"""Processing-time coverage: on a repo large enough for the difference to be unambiguous, an
incremental resync must do far less work than a full recompute.

Per the suite's policy, the hard gates are STRUCTURAL (deterministic): the incremental path
parses only the blast radius while a full recompute parses everything. Wall-clock is recorded
and checked only as a lenient relative comparison (incremental < full), which is stable when
both run back-to-back on the same machine at this scale.
"""

from __future__ import annotations

import pytest

from ._helpers import GitRepo, drop, fresh_graph, ingest_repo, python_report, timed
from auryn.core.graph_client import GraphWriter

N_FILES = 150


def _synthetic() -> dict[str, str]:
    """N independent modules + one hub that imports a few — enough that parsing dominates a
    full recompute, so skipping it is visibly cheaper."""
    files = {f"mod_{i:03d}.py": f"def f_{i:03d}(x):\n    return x + {i}\n" for i in range(N_FILES)}
    hub = "".join(f"from mod_{i:03d} import f_{i:03d}\n" for i in range(5))
    hub += "\n\ndef hub():\n    return " + " + ".join(f"f_{i:03d}(0)" for i in range(5)) + "\n"
    files["hub.py"] = hub
    return files


@pytest.fixture
def big_repo(tmp_path):
    return GitRepo.init(tmp_path / "big").write(_synthetic())


def test_incremental_does_far_less_work(big_repo, falkordb_connection, tmp_path, capsys):
    cache = tmp_path / "cache"
    total = N_FILES + 1
    sha1 = big_repo.commit("v1")
    g = fresh_graph(falkordb_connection, "timing")
    f = fresh_graph(falkordb_connection, "timing_full")
    try:
        ingest_repo(GraphWriter(g), big_repo, sha1, cache, project="timing_proj")  # cold (full)

        big_repo.write({"mod_000.py": "def f_000(x):\n    return x + 100000\n"})
        sha2 = big_repo.commit("v2")

        incr = timed(lambda: ingest_repo(GraphWriter(g), big_repo, sha2, cache, project="timing_proj"))
        full = timed(lambda: ingest_repo(GraphWriter(f), big_repo, sha2, cache, project="timing_proj"))

        incr_py = python_report(incr.result).result
        full_py = python_report(full.result).result

        # ── structural gates (deterministic) ───────────────────────────────────────────
        assert incr_py.files_parsed <= 5, f"incremental parsed {incr_py.files_parsed}, expected the blast radius"
        assert incr_py.files_cached >= total - 5, "incremental should serve almost everything from the graph"
        assert full_py.files_parsed >= total - 1, f"full recompute should parse ~all {total} files"
        assert full_py.files_cached == 0, "a full recompute caches nothing (ast engine)"

        # ── recorded wall-clock + lenient relative check ──────────────────────────────
        with capsys.disabled():
            print(f"\n[timing] full={full.seconds*1000:.0f}ms  incremental={incr.seconds*1000:.0f}ms  "
                  f"speedup={full.seconds/incr.seconds:.1f}x  "
                  f"(parsed: full={full_py.files_parsed} incr={incr_py.files_parsed})")
        assert incr.seconds < full.seconds, "incremental should be faster than a full recompute at this scale"
    finally:
        drop(g, f)
