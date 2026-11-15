"""C full-recompute vs. AUTHORED-SHARED incremental resync, head-to-head, per phase (ADR 0005).

The measured payoff for the C delta-sync work: cold-ingest a real C tree (with a planted `.h`/`.c`
sentinel), then for each small edit run BOTH a full `sync_at` and an `incremental_c_sync` from the
same warm baseline and print their phase breakdowns side by side. Correctness (incremental == full)
is proven separately by tests/sync/test_delta_differential.py + test_delta_cross_repo.py; this only
times them.

Run:
    uv run python benchmarks/freshness/measure_c_incremental.py --repo-dir <path-to-c-src>
    uv run python benchmarks/freshness/measure_c_incremental.py --repo-uri https://github.com/redis/redis.git --ref 7.4.0
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from measure_c_baseline import _SENTINEL_C, _prepare  # noqa: E402
from run_freshness_benchmark import Config, connect_writer  # noqa: E402

from auryn.core.instrument import record_phases  # noqa: E402
from auryn.core.local_source import LocalDirectorySource  # noqa: E402
from auryn.ingestors.c.git_sync import sync_at  # noqa: E402
from auryn.ingestors.c.incremental import ChangeSet, incremental_c_sync  # noqa: E402

_NAME = "cbench"


def _full(writer, ws):
    src = LocalDirectorySource(ws)
    with record_phases() as ph:
        s = sync_at(writer, src, src.tree_sha(), _NAME, "measure://x", None)
    return s.elapsed_seconds, dict(ph), s


def _incr(writer, ws, changes):
    src = LocalDirectorySource(ws)
    with record_phases() as ph:
        s = incremental_c_sync(writer, src, src.tree_sha(), changes, project_name=_NAME, repo_uri="measure://x")
    return s.elapsed_seconds, dict(ph), s


def _rewrite(p: Path, old: str, new: str):
    b = p.read_bytes()
    p.write_text(b.decode().replace(old, new, 1))
    return lambda: p.write_bytes(b)


def _append(p: Path, s: str):
    b = p.read_bytes()
    p.write_bytes(b + s.encode())
    return lambda: p.write_bytes(b)


def _scenarios(ws: Path):
    c = ws / _SENTINEL_C
    cs = ChangeSet(modified=[_SENTINEL_C])
    return [
        ("edit_body", lambda: _rewrite(c, "return value + 0;", "return value + 424242;"), cs),
        ("add_function", lambda: _append(c, "\nint bench_added(int v) { return v * 2; }\n"), cs),
        ("comment_eof", lambda: _append(c, "\n/* bench comment */\n"), cs),
    ]


def main(argv):
    cfg = Config.from_args(argv)  # project_name unused here (_full/_incr hardcode _NAME)
    tmp, ws = _prepare(cfg, cfg.n_functions)
    writer, cleanup = connect_writer(cfg, tmp)
    try:
        tf, phf, sf = _full(writer, ws)  # cold ingest
        print(f"cold full ingest: {tf:.3f}s  files={sf.files_parsed}")
        rows = []
        for name, mutate, changes in _scenarios(ws):
            undo = mutate();  tf, phf, sf = _full(writer, ws);  undo();  _full(writer, ws)
            undo = mutate();  ti, phi, si = _incr(writer, ws, changes);  undo();  _full(writer, ws)
            readf = lambda ph: ph.get("read_fingerprints", 0)
            rows.append((name, tf, ti, round(tf / ti, 1) if ti else 0, si.files_parsed, si.files_cached,
                         round(readf(phf), 3), round(readf(phi), 3)))
        w = max(len(r[0]) for r in rows)
        print(f"\n{'scenario':<{w}}  {'full_s':>8}  {'incr_s':>8}  {'speedup':>8}  {'parsed':>7}  {'cached':>7}  {'rdbk_full':>9}  {'rdbk_incr':>9}")
        print("-" * (w + 70))
        for name, tf, ti, sp, par, cac, rf, ri in rows:
            print(f"{name:<{w}}  {tf:>8.3f}  {ti:>8.3f}  {sp:>7}x  {par:>7}  {cac:>7}  {rf:>9.3f}  {ri:>9.3f}")
    finally:
        cleanup()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main(sys.argv[1:])
