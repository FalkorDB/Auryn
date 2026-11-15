"""Bucketed A/B/C/D report + the pre-registered pass/fail gate for the injection hook.

The A/B findings show the *sign* of the token effect flips with task difficulty (graph overhead
loses on trivial greppable questions, wins big on hard transitive ones). So we never judge the
hook on a blended average -- we bucket by difficulty (easy vs hard) and hold it to a gate fixed
BEFORE the run:

  G1  no quality regression: coverage(C) >= coverage(A) in EVERY bucket
  G2  real hard-bucket lift:  coverage(C) - coverage(A) >= HARD_MARGIN on the hard bucket
  G3  easy-bucket overhead ceiling: tokens(C) <= tokens(A) * (1 + EASY_TOKEN_CEILING)

Arms: A=bare, B=MCP+priming (adopt-or-not), C=hook injection (zero behaviour change), D=hook+MCP.

Usage:
    uv run python benchmarks/agent/abcd_report.py --bucket easy:results/<stamp1> --bucket hard:results/<stamp2>
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

HARD_MARGIN = 0.15          # coverage points C must beat A by on the hard bucket
EASY_TOKEN_CEILING = 0.10   # C may cost at most +10% tokens vs A on the easy bucket


def load_runs(result_dir: str) -> list[dict]:
    p = Path(result_dir)
    summ = p / "summary.json"
    if summ.exists():
        return json.loads(summ.read_text()).get("runs", [])
    # fall back to individual run logs
    runs = []
    for f in sorted((p / "runs").glob("*.json")):
        r = json.loads(f.read_text())
        r = {**r, "coverage": (r.get("grade") or {}).get("coverage")}
        runs.append(r)
    return runs


def _mean(xs: list[float]) -> float:
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else math.nan


def agg(runs: list[dict], arm: str) -> dict:
    rs = [r for r in runs if r["arm"] == arm]
    if not rs:
        return {}
    return {
        "n": len(rs),
        "tokens": _mean([r["tokens"]["total"] for r in rs]),
        "cost": _mean([r.get("cost_usd") or 0 for r in rs]),
        "time": _mean([r["wall_seconds"] for r in rs]),
        "turns": _mean([r.get("num_turns") or 0 for r in rs]),
        "mcp": _mean([r["tool_mix"]["mcp"] for r in rs]),
        "file": _mean([r["tool_mix"]["file"] for r in rs]),
        "coverage": _mean([r.get("coverage") for r in rs]),
        "fires": _mean([(r.get("hook") or {}).get("fires", 0) for r in rs]),
        "inj_tok": _mean([(r.get("hook") or {}).get("injected_tokens", 0) for r in rs]),
        "influence": _mean([(r.get("hook_influence") or {}).get("rate") for r in rs]),
        # adoption: fraction of runs that actually invoked a graph MCP tool (arms B/D)
        "adopted": _mean([1.0 if r["tool_mix"]["mcp"] > 0 else 0.0 for r in rs]),
    }


def print_bucket(name: str, runs: list[dict]) -> dict:
    print(f"\n=== bucket: {name}  ({len(runs)} runs) ===")
    hdr = f"{'arm':<4}{'tokens':>10}{'cost$':>8}{'time_s':>8}{'turns':>7}{'mcp/file':>10}{'cover':>7}{'adopt':>7}{'fires':>7}{'inj_tok':>9}{'infl':>7}"
    print(hdr)
    cells = {}
    for arm in ("A", "B", "C", "D"):
        a = agg(runs, arm)
        cells[arm] = a
        if not a:
            continue
        infl = "—" if (a["influence"] != a["influence"]) else f"{a['influence']:.2f}"
        print(f"{arm:<4}{a['tokens']:>10,.0f}{a['cost']:>8.3f}{a['time']:>8.1f}{a['turns']:>7.1f}"
              f"{a['mcp']:>5.1f}/{a['file']:<4.1f}{a['coverage']:>7.2f}{a['adopted']:>7.0%}"
              f"{a['fires']:>7.1f}{a['inj_tok']:>9,.0f}{infl:>7}")
    return cells


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", action="append", required=True,
                    help="name:result_dir, e.g. easy:results/20260919-... (repeatable)")
    args = ap.parse_args()

    buckets: dict[str, dict] = {}
    for spec in args.bucket:
        name, _, path = spec.partition(":")
        buckets[name] = print_bucket(name, load_runs(path))

    # ── the gate ──────────────────────────────────────────────────────────────────────────
    print("\n=== pre-registered pass/fail gate (A vs C) ===")
    verdicts = []

    def cov(bucket, arm):
        return (buckets.get(bucket, {}).get(arm) or {}).get("coverage")

    def tok(bucket, arm):
        return (buckets.get(bucket, {}).get(arm) or {}).get("tokens")

    # G1 -- no regression in any bucket
    g1 = True
    for b in buckets:
        cA, cC = cov(b, "A"), cov(b, "C")
        if cA is not None and cC is not None and cC < cA - 1e-9:
            g1 = False
            print(f"  G1 FAIL: coverage(C)={cC:.2f} < coverage(A)={cA:.2f} in bucket '{b}'")
    verdicts.append(("G1 no-regression (C>=A every bucket)", g1))

    # G2 -- real lift on the hard bucket
    cA, cC = cov("hard", "A"), cov("hard", "C")
    if cA is None or cC is None:
        g2 = None
        print("  G2 SKIP: no 'hard' bucket data")
    else:
        g2 = (cC - cA) >= HARD_MARGIN
        print(f"  G2 hard lift: coverage(C)-coverage(A) = {cC - cA:+.2f} (need >= {HARD_MARGIN})")
    verdicts.append((f"G2 hard-bucket lift >= {HARD_MARGIN}", g2))

    # G3 -- overhead ceiling on the easy bucket
    tA, tC = tok("easy", "A"), tok("easy", "C")
    if tA is None or tC is None:
        g3 = None
        print("  G3 SKIP: no 'easy' bucket data")
    else:
        g3 = tC <= tA * (1 + EASY_TOKEN_CEILING)
        print(f"  G3 easy overhead: tokens(C)/tokens(A) = {tC / tA:.2f}x (ceiling {1 + EASY_TOKEN_CEILING}x)")
    verdicts.append((f"G3 easy-bucket overhead <= {1 + EASY_TOKEN_CEILING}x", g3))

    # cost-per-quality on the hard bucket (the justification number)
    if cov("hard", "A") is not None and cov("hard", "C") is not None:
        dcov = cov("hard", "C") - cov("hard", "A")
        dtok = tok("hard", "C") - tok("hard", "A")
        if abs(dcov) > 1e-9:
            print(f"\n  hard-bucket Δtokens/Δcoverage = {dtok / dcov:+,.0f} tokens per coverage-point"
                  f"  ({'cheaper AND better' if dtok <= 0 and dcov > 0 else 'trade-off'})")

    print("\n  " + "\n  ".join(
        f"{'PASS' if v else ('SKIP' if v is None else 'FAIL')}  {name}" for name, v in verdicts))
    hard_fails = [v for _n, v in verdicts if v is False]
    print(f"\n  OVERALL: {'SHIP' if not hard_fails else 'DO NOT SHIP (gate failed)'}")


if __name__ == "__main__":
    main()
