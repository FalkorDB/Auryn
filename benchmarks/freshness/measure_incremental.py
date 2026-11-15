"""Full-recompute vs. incremental resync, head-to-head, per phase (ADR 0002).

Cold-ingests a real Python tree (with a planted sentinel module), then for each small edit
runs BOTH a full `sync_at` and an `incremental_ast_sync` from the same warm baseline and
prints their phase breakdowns side by side. This is the payoff measurement for the
incremental prototype: it should show parse and read-back collapsing from O(repo) to
O(blast-radius) while the resulting delta is identical (correctness is proven separately by
tests/ingestors/test_python_incremental_equivalence.py).

Run:
    uv run python benchmarks/freshness/measure_incremental.py \
        --repo-uri https://github.com/django/django.git --ref 5.1 --project-name django
    uv run python benchmarks/freshness/measure_incremental.py --repo-dir packages
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from report import print_summary  # noqa: E402

from auryn.core.graph_client import FalkorDBConfig, GraphWriter, connect  # noqa: E402
from auryn.core.instrument import record_phases  # noqa: E402
from auryn.core.local_source import LocalDirectorySource  # noqa: E402
from auryn.ingestors.python.git_sync import GitPythonProjectConfig, sync_at  # noqa: E402
from auryn.ingestors.python.incremental import ChangeSet, incremental_ast_sync  # noqa: E402

from run_freshness_benchmark import _sentinel_source, _server_up, _COPY_IGNORE  # noqa: E402

logger = logging.getLogger("measure_incremental")
_SENTINEL = "zzz_freshness_bench_sentinel.py"


def _prepare(args) -> tuple[Path, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="incr_measure_"))
    ws = tmp / "workspace"
    if args.repo_dir:
        shutil.copytree(Path(args.repo_dir).resolve(), ws, ignore=_COPY_IGNORE,
                        symlinks=False, ignore_dangling_symlinks=True)
    else:
        cmd = ["git", "clone", "--depth", "1"]
        if args.ref:
            cmd += ["--branch", args.ref]
        cmd += [args.repo_uri, str(ws)]
        subprocess.run(cmd, check=True)
        shutil.rmtree(ws / ".git", ignore_errors=True)
    (ws / _SENTINEL).write_text(_sentinel_source(args.n_functions))
    return tmp, ws


def _connect(args, tmp: Path) -> GraphWriter:
    mode = args.falkordb
    if mode == "auto":
        mode = "localhost:6379" if _server_up("localhost", 6379) else "embedded"
    name = f"incr_{uuid4().hex[:8]}"
    if mode == "embedded":
        conf = FalkorDBConfig(embedded=True, db_path=str(tmp / "g.db"), graph_name=name)
    else:
        host, _, port = mode.partition(":")
        conf = FalkorDBConfig(host=host or "localhost", port=int(port or 6379), graph_name=name)
    return GraphWriter(connect(conf))


def _config(name: str) -> GitPythonProjectConfig:
    return GitPythonProjectConfig(repo_uri="measure://x", project_name=name)


def _full(writer: GraphWriter, ws: Path, cfg) -> tuple[float, dict]:
    src = LocalDirectorySource(ws)
    with record_phases() as ph:
        stats = sync_at(writer, cfg, src, src.tree_sha(), None, engine="ast")
    return stats.elapsed_seconds, dict(ph)


def _incr(writer: GraphWriter, ws: Path, cfg, changes: ChangeSet) -> tuple[float, dict, object]:
    src = LocalDirectorySource(ws)
    with record_phases() as ph:
        stats = incremental_ast_sync(writer, cfg, src, src.tree_sha(), changes)
    return stats.elapsed_seconds, dict(ph), stats


def _row(total: float, ph: dict) -> dict:
    apply_s = ph.get("read_fingerprints", 0) + ph.get("diff", 0) + ph.get("write", 0)
    return {
        "total_s": round(total, 3),
        "parse_s": round(ph.get("parse", 0), 3),
        "resolve_s": round(ph.get("resolve", 0), 3),
        "closure_s": round(ph.get("closure", 0), 3),
        "index_seed_s": round(ph.get("index_seed", 0), 3),
        "read_fingerprints_s": round(ph.get("read_fingerprints", 0), 3),
        "diff_s": round(ph.get("diff", 0), 3),
        "write_s": round(ph.get("write", 0), 3),
    }


# Each scenario: a mutate + its inverse (undo), and the ChangeSet git would report.
def _scenarios():
    def append(text):
        def mut(ws):
            p = ws / _SENTINEL
            before = p.read_text()
            p.write_text(before + text)
            return lambda: p.write_text(before)
        return mut

    def replace(old, new):
        def mut(ws):
            p = ws / _SENTINEL
            before = p.read_text()
            assert old in before
            p.write_text(before.replace(old, new, 1))
            return lambda: p.write_text(before)
        return mut

    cs = ChangeSet(modified=[_SENTINEL])
    return [
        ("comment_eof", append("\n# trailing comment\n"), cs),
        ("edit_body", replace("def bench_fn_000(value):\n    return value + 0",
                              "def bench_fn_000(value):\n    return value + 987654"), cs),
        ("add_function", append("\n\ndef bench_added(value):\n    return value * 2\n"), cs),
    ]


def main(argv):
    p = argparse.ArgumentParser()
    p.add_argument("--repo-dir", default=os.environ.get("FRESHNESS_BENCH_REPO_DIR"))
    p.add_argument("--repo-uri", default=os.environ.get("FRESHNESS_BENCH_REPO_URI"))
    p.add_argument("--ref", default=os.environ.get("FRESHNESS_BENCH_REF"))
    p.add_argument("--project-name", default="incr-measure")
    p.add_argument("--falkordb", default="auto")
    p.add_argument("--n-functions", type=int, default=40)
    args = p.parse_args(argv)
    if not args.repo_dir and not args.repo_uri:
        p.error("one of --repo-dir/--repo-uri required")
    logging.basicConfig(level=logging.WARNING)

    tmp, ws = _prepare(args)
    writer = _connect(args, tmp)
    cfg = _config(args.project_name)
    try:
        t, ph = _full(writer, ws, cfg)  # cold ingest (warms the graph)
        print_summary("Cold ingest (full)", _row(t, ph))

        rows = []
        for name, mutate, changes in _scenarios():
            undo = mutate(ws)  # measure FULL
            tf, phf = _full(writer, ws, cfg)
            undo()
            _full(writer, ws, cfg)  # restore graph to baseline

            undo = mutate(ws)  # measure INCREMENTAL from the same baseline
            ti, phi, stats = _incr(writer, ws, cfg, changes)
            undo()
            _full(writer, ws, cfg)  # restore

            speedup = round(tf / ti, 1) if ti > 0 else float("inf")
            print_summary(f"[{name}] FULL", _row(tf, phf))
            print_summary(f"[{name}] INCREMENTAL  (parsed={stats.files_parsed}, cached={stats.files_cached}, "
                          f"Δnodes={stats.nodes_added + stats.nodes_updated + stats.nodes_removed}, "
                          f"Δedges={stats.edges_added + stats.edges_updated + stats.edges_removed})", _row(ti, phi))
            rows.append((name, tf, ti, speedup, stats.files_parsed, stats.files_cached))

        w = max(len(n) for n, *_ in rows)
        print("\n" + "=" * 72)
        print(f"{'scenario':<{w}}  {'full_s':>8}  {'incr_s':>8}  {'speedup':>8}  {'parsed':>7}  {'cached':>7}")
        print("-" * 72)
        for name, tf, ti, sp, parsed, cached in rows:
            print(f"{name:<{w}}  {tf:>8.3f}  {ti:>8.3f}  {sp:>7}x  {parsed:>7}  {cached:>7}")
        print("=" * 72)
    finally:
        try:
            writer._graph.delete()
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main(sys.argv[1:])
