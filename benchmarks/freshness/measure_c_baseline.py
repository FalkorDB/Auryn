"""C resync-cost baseline (ADR 0005 — delta-sync generalization).

C is NOT on the incremental engine yet (`route_incremental` covers only python-ast / ts /
bash). So this measures the *current* cost of keeping a C graph fresh: the full
recompute-and-diff that `ingest_directory` runs today, per small edit, broken down by phase —
the exact "before" number ADR 0005 must beat, and the same methodology as
`run_freshness_benchmark.py` (which this reuses wholesale: same production `ingest_directory`
path, same phase instrumentation, same one-warm-baseline-replayed discipline). The only
C-specific parts are the planted sentinel (a `.h`/`.c` pair, so the declaration/definition
split is exercised) and the scenario mutations.

The headline the delta design has to move: how much of a one-line C edit's cost is
`parse` (re-parsing every unchanged file — O(repo), what delta-sync collapses to
O(change+closure)) and `read_fingerprints` (whole-subgraph read-back — O(graph), what a
provenance-scoped read-back collapses to O(blast-radius)).

Run:
    # fastest: a local C tree (no network)
    uv run python benchmarks/freshness/measure_c_baseline.py --repo-dir <path-to-c-src> \
        --project-name redis --out benchmarks/results/freshness-c.json
    # or clone one:
    uv run python benchmarks/freshness/measure_c_baseline.py \
        --repo-uri https://github.com/redis/redis.git --ref 7.4.0 --project-name redis
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# reuse the freshness harness verbatim (same dir on sys.path via the block below)
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from run_freshness_benchmark import (  # noqa: E402
    Config,
    Metrics,
    _COPY_IGNORE,
    _resync,
    _restore,
    connect_writer,
    print_matrix,
    write_json,
)

logger = logging.getLogger("measure_c_baseline")

_SENTINEL_C = "zzz_bench_sentinel.c"
_SENTINEL_H = "zzz_bench_sentinel.h"
_ADDED_C = "zzz_bench_added.c"


def _sentinel_sources(n: int) -> tuple[str, str]:
    """A `.h` that declares the first few functions + the caller (the cross-file DECLARES/
    DEFINES split), and a `.c` that defines all n and a caller that calls the first few (CALLS
    edges to churn). Deterministic strings the mutations below match exactly."""
    n_decl = min(5, n)
    h = ["#ifndef ZZZ_BENCH_SENTINEL_H", "#define ZZZ_BENCH_SENTINEL_H", ""]
    for i in range(n_decl):
        h.append(f"int bench_fn_{i:03d}(int value);")
    h += ["int bench_caller(void);", "", "#endif", ""]

    c = [f'#include "{_SENTINEL_H}"', ""]
    for i in range(n):
        c += [f"int bench_fn_{i:03d}(int value) {{", f"    return value + {i};", "}", ""]
    c += ["int bench_caller(void) {", "    int total = 0;"]
    for i in range(min(5, n)):
        c += [f"    total += bench_fn_{i:03d}(total);"]
    c += ["    return total;", "}", ""]
    return "\n".join(h), "\n".join(c)


def _prepare(cfg: Config, n: int) -> tuple[Path, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="c_baseline_"))
    ws = tmp / "workspace"
    if cfg.repo_dir:
        shutil.copytree(Path(cfg.repo_dir).expanduser().resolve(), ws, ignore=_COPY_IGNORE,
                        symlinks=False, ignore_dangling_symlinks=True)
    else:
        cmd = ["git", "clone", "--depth", "1"]
        if cfg.ref:
            cmd += ["--branch", cfg.ref]
        cmd += [cfg.repo_uri, str(ws)]
        subprocess.run(cmd, check=True)
        shutil.rmtree(ws / ".git", ignore_errors=True)
    h, c = _sentinel_sources(n)
    (ws / _SENTINEL_H).write_text(h)
    (ws / _SENTINEL_C).write_text(c)
    return tmp, ws


# ── scenarios: mutate the workspace, return an undo() closure ─────────────────────────
def _rewrite(path: Path, old: str, new: str):
    before = path.read_bytes()
    text = before.decode()
    if old not in text:
        raise RuntimeError(f"scenario setup: {old!r} not in {path.name}")
    path.write_text(text.replace(old, new, 1))
    return lambda: path.write_bytes(before)


def _append(path: Path, suffix: str):
    before = path.read_bytes()
    path.write_bytes(before + suffix.encode())
    return lambda: path.write_bytes(before)


def _scenarios(n: int):
    last = n - 1
    return [
        ("noop", "Re-ingest, zero changes (tree-sha short-circuit)",
         lambda ws: (lambda: None)),
        ("comment_eof", "Add a trailing comment to one .c (no entity shift)",
         lambda ws: _append(ws / _SENTINEL_C, "\n/* bench trailing comment */\n")),
        ("edit_body", "Change one function's body (CALLS/return churn)",
         lambda ws: _rewrite(ws / _SENTINEL_C, "int bench_fn_000(int value) {\n    return value + 0;",
                             "int bench_fn_000(int value) {\n    return value + 424242;")),
        ("add_function", "Add one new function to a .c",
         lambda ws: _append(ws / _SENTINEL_C, "\nint bench_fn_added(int value) {\n    return value * 2;\n}\n")),
        ("delete_function", "Delete one function from a .c",
         lambda ws: _rewrite(ws / _SENTINEL_C,
                             f"int bench_fn_{last:03d}(int value) {{\n    return value + {last};\n}}\n", "")),
        ("edit_header", "Change the .h (add a prototype) — the .h/.c split case",
         lambda ws: _rewrite(ws / _SENTINEL_H, "int bench_caller(void);",
                             "int bench_fn_new(int value);\nint bench_caller(void);")),
        ("add_file", "Add one new .c file",
         lambda ws: _add_file(ws)),
        ("delete_file", "Delete one .c file",
         lambda ws: _delete_file(ws / _SENTINEL_C)),
    ]


def _add_file(ws: Path):
    p = ws / _ADDED_C
    _, c = _sentinel_sources(5)
    p.write_text(c.replace("bench_fn_", "added_fn_").replace(f'#include "{_SENTINEL_H}"\n', ""))
    return lambda: p.unlink(missing_ok=True)


def _delete_file(p: Path):
    before = p.read_bytes()
    p.unlink()
    return lambda: p.write_bytes(before)


def _measure_parse(ws: Path) -> tuple[int, float, float]:
    """Time C parse directly (the C ingestor lumps parse into `other_s`, not a `parse` phase).
    Returns (n_files, total_parse_s, per_file_parse_s) — the O(repo) parse a full recompute
    pays on every edit and that delta-sync replaces with O(change+closure)."""
    from auryn.ingestors.c.file_parser import parse_source
    from auryn.ingestors.c.plugin import EXCLUDED_DIR_NAMES, FILE_SUFFIXES

    excl = set(EXCLUDED_DIR_NAMES)
    paths = sorted({p for suf in FILE_SUFFIXES for p in ws.rglob(f"*{suf}")
                    if not (excl & set(p.relative_to(ws).parts))})
    t0 = time.monotonic()
    for p in paths:
        parse_source(p.read_bytes(), str(p.relative_to(ws)))
    total = time.monotonic() - t0
    return len(paths), round(total, 4), (total / max(1, len(paths)))


def _projection(cold: Metrics, m: Metrics, full_parse_s: float, per_file_parse_s: float) -> dict:
    """Project a delta-sync run for this scenario from the measured phases + the directly-timed
    parse. parse collapses O(repo)→O(change+closure); read-back stays full (v1) or is
    provenance-scoped (v2). k = files a 1-edit blast radius parses (change + a few callers)."""
    files = max(1, cold.files_parsed)
    k = 3  # changed file + a small reverse-dep closure (conservative)
    frac = k / files
    delta_parse = round(per_file_parse_s * k, 4)
    current_full = m.total_s
    # v1 = scope PARSE only (keep whole-graph readback + write — the O(graph) reconcile).
    v1 = round(delta_parse + m.read_fingerprints_s + m.diff_s + m.write_s, 3)
    # v2 = scope parse + readback + write to the blast radius (a scoped delta never re-emits
    # unchanged files, so it can't produce their edge-churn; readback/write ∝ blast radius).
    scoped_readback = m.read_fingerprints_s * frac
    scoped_write = m.write_s * frac
    v2 = round(delta_parse + scoped_readback + m.diff_s + scoped_write, 3)
    return {
        "measured_full_s": round(current_full, 3),
        "other_s(parse+walk)": m.other_s,
        "readback_s": m.read_fingerprints_s,
        "write_s": m.write_s,
        "proj_delta_v1_s": v1,
        "proj_delta_v2_s": v2,
        "proj_speedup_v2": round(current_full / max(1e-6, v2), 1),
    }


def main(argv):
    cfg = Config.from_args(argv)
    logging.basicConfig(level=logging.INFO if cfg.verbose else logging.WARNING)
    logger.setLevel(logging.INFO)
    n = cfg.n_functions

    tmp, ws = _prepare(cfg, n)
    writer, cleanup = connect_writer(cfg, tmp)
    started = time.monotonic()
    try:
        cold = _resync(writer, ws, cfg, "cold", "Full cold ingest (warms graph + parse)")
        if cold.nodes_added == 0:
            raise SystemExit("cold ingest added no nodes — is --repo-dir real C source?")
        n_files, full_parse_s, per_file_parse_s = _measure_parse(ws)

        results: list[Metrics] = []
        projections = []
        for name, desc, make in _scenarios(n):
            undo = make(ws)
            try:
                m = _resync(writer, ws, cfg, name, desc)
            finally:
                undo()
                _restore(writer, ws, cfg)
            results.append(m)
            projections.append((name, _projection(cold, m, full_parse_s, per_file_parse_s)))

        print_matrix(cold, results)
        print("\nDELTA-SYNC PROJECTION (measured phases + directly-timed parse; k=3 blast-radius files)")
        print("  v1 = scope parse only | v2 = scope parse+readback+write to the blast radius")
        print("-" * 108)
        print(f"{'scenario':<16}{'full_s':>10}{'parse':>9}{'readback':>10}{'write':>9}"
              f"{'Δ v1_s':>10}{'Δ v2_s':>10}{'v2 speedup':>12}")
        for name, pr in projections:
            print(f"{name:<16}{pr['measured_full_s']:>10.3f}{pr['other_s(parse+walk)']:>9.3f}"
                  f"{pr['readback_s']:>10.3f}{pr['write_s']:>9.3f}{pr['proj_delta_v1_s']:>10.3f}"
                  f"{pr['proj_delta_v2_s']:>10.3f}{pr['proj_speedup_v2']:>11.1f}x")
        print("-" * 108)
        print(f"cold ingest: {cold.total_s:.3f}s, files={cold.files_parsed}, nodes={cold.nodes_added}, "
              f"edges~{cold.edges_added}")
        print(f"DIRECT C PARSE: {n_files} files, full-parse={full_parse_s:.3f}s "
              f"(~{1000*per_file_parse_s:.1f} ms/file) — this is the O(repo) cost a full recompute "
              f"re-pays every edit; delta parses ~3 → ~{1000*per_file_parse_s*3:.0f} ms.")
        if cfg.out:
            write_json(cfg.out, cfg, cold, results)
        logger.info("done in %.1fs", time.monotonic() - started)
    finally:
        cleanup()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main(sys.argv[1:])
