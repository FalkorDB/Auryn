"""Overhead micro-benchmark for the PostToolUse enrichment hook -- the cheap go/no-go gate.

Before spending anything on agent A/B/C/D runs, this answers the one question the whole feature
lives or dies on: *how often does the hook fire, and how much does each fire cost?* It replays a
corpus of real reflex-tool calls (Grep/Read/Bash/Edit) through the hook's pure ``enrich()`` core
against a live graph and reports:

  * **fire rate** -- of all reflex calls, how many produced an injection (the rest are silent);
  * **suppression breakdown** -- *why* the silent ones stayed silent (not-a-symbol, ambiguous,
    no-graph-hit, deduped, ...), so we can see the gate working;
  * **injected-token distribution** -- the added context cost per fire (mean/median/p90/max);
  * **query latency** -- warm per-call graph time (p50/p95). The *real* hook also pays a fixed
    process-start cost each call; ``--measure-coldstart`` times that separately via the CLI.

Two corpora, combinable:
  --from-transcripts DIR   harvest tool_use inputs from existing A/B run logs (real agent behaviour)
  --sample-from-graph N    synthesise N Grep (symbol) + N Read (file) events from the graph itself

Usage:
    uv run python benchmarks/hook/replay.py --graph django --repo /tmp/django \
        --from-transcripts benchmarks/agent/results-swe --sample-from-graph 200
"""

from __future__ import annotations

import argparse
import glob
import json
import subprocess
import time
from pathlib import Path

from auryn.hooks import postuse_enrich as hook

_REFLEX_TOOLS = {"Grep", "Read", "Bash", "Edit", "Write", "MultiEdit"}


# ── corpora ──────────────────────────────────────────────────────────────────────────────


def harvest_transcripts(dirs: list[str], repo: str) -> list[dict]:
    """Every reflex tool_use recorded in the run logs under ``dirs`` -> replayable events. The
    A/B harness stores each run's distilled ``timeline`` (see run_ab.parse_stream); we pull the
    tool_use entries whose name is a reflex tool."""
    events: list[dict] = []
    seen_files: set[str] = set()
    for d in dirs:
        for pattern in (f"{d}/**/runs/*.json", f"{d}/**/*.json"):
            for p in glob.glob(pattern, recursive=True):
                if p in seen_files:
                    continue
                seen_files.add(p)
                try:
                    data = json.loads(Path(p).read_text())
                except Exception:
                    continue
                for e in data.get("timeline") or []:
                    if e.get("kind") == "tool_use" and e.get("name") in _REFLEX_TOOLS:
                        events.append(
                            {"tool_name": e["name"], "tool_input": e.get("input") or {},
                             "session_id": "replay", "cwd": repo}
                        )
    return events


def sample_from_graph(ctx: hook.GraphContext, repo: str, n: int) -> list[dict]:
    """Synthetic events straight from the graph: ``n`` Grep-by-name events over real symbol names
    and ``n`` Read events over real file paths. A controlled corpus for measuring fire rate and
    latency when transcripts are thin or absent."""
    graph = ctx.graph
    if graph is None:
        return []
    events: list[dict] = []
    try:
        rows = graph.ro_query(
            "MATCH (n) WHERE n.name IS NOT NULL AND NOT n.id STARTS WITH 'extern://' "
            "AND labels(n)[0] IN ['Function','Method','Class'] RETURN n.name LIMIT $n",
            {"n": n},
        ).result_set
        for (name,) in rows:
            events.append({"tool_name": "Grep", "tool_input": {"pattern": name},
                           "session_id": "replay", "cwd": repo})
    except Exception:
        pass
    try:
        rows = graph.ro_query(
            "MATCH (n) WHERE n.file_path IS NOT NULL RETURN DISTINCT n.file_path LIMIT $n", {"n": n}
        ).result_set + graph.ro_query(
            "MATCH (m) WHERE m.path IS NOT NULL RETURN DISTINCT m.path LIMIT $n", {"n": n}
        ).result_set
        for (path,) in rows:
            abspath = path if Path(path).is_absolute() else str(Path(repo) / path)
            events.append({"tool_name": "Read", "tool_input": {"file_path": abspath},
                           "session_id": "replay", "cwd": repo})
    except Exception:
        pass
    return events


# ── replay ───────────────────────────────────────────────────────────────────────────────


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    i = min(len(s) - 1, int(round((p / 100) * (len(s) - 1))))
    return s[i]


def replay(events: list[dict], cfg: hook.HookConfig) -> dict:
    """Run every event through enrich() with ONE shared GraphContext (so connection cost is paid
    once -- see --measure-coldstart for the per-process cost the real hook pays each call)."""
    ctx = hook.GraphContext(cfg)
    by_tool: dict[str, dict] = {}
    reasons: dict[str, int] = {}
    tokens: list[int] = []
    warm_latency: list[float] = []
    cold_latency = None
    for idx, ev in enumerate(events):
        t = time.perf_counter()
        r = hook.enrich(ev, ctx, cfg)
        dt = (time.perf_counter() - t) * 1000
        if idx == 0:
            cold_latency = dt  # first call includes connect+status inside this process
        else:
            warm_latency.append(dt)
        tool = ev["tool_name"]
        bucket = by_tool.setdefault(tool, {"n": 0, "fired": 0, "tokens": 0})
        bucket["n"] += 1
        reasons[r.reason] = reasons.get(r.reason, 0) + 1
        if r.fired:
            bucket["fired"] += 1
            bucket["tokens"] += r.injected_tokens
            tokens.append(r.injected_tokens)
    n = len(events)
    fired = sum(b["fired"] for b in by_tool.values())
    return {
        "events": n,
        "fired": fired,
        "fire_rate": round(fired / n, 3) if n else 0.0,
        "by_tool": {
            t: {**b, "fire_rate": round(b["fired"] / b["n"], 3) if b["n"] else 0.0}
            for t, b in sorted(by_tool.items())
        },
        "suppression_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        "injected_tokens": {
            "total": sum(tokens),
            "per_fire_mean": round(sum(tokens) / len(tokens), 1) if tokens else 0,
            "per_fire_median": _pct([float(t) for t in tokens], 50),
            "per_fire_p90": _pct([float(t) for t in tokens], 90),
            "per_fire_max": max(tokens) if tokens else 0,
        },
        "warm_query_latency_ms": {
            "p50": round(_pct(warm_latency, 50), 2),
            "p95": round(_pct(warm_latency, 95), 2),
            "max": round(max(warm_latency), 2) if warm_latency else 0.0,
        },
        "cold_first_call_ms": round(cold_latency, 2) if cold_latency is not None else None,
    }


def measure_coldstart(graph: str, repo: str, reps: int = 5) -> dict:
    """The real per-call cost: Claude Code spawns the hook as a fresh process for every tool call,
    so each pays Python start + import + connect. Time that end-to-end via the actual CLI."""
    import os

    env = dict(os.environ, AURYN_HOOK_GRAPH=graph, AURYN_HOOK_METRICS="/dev/null")
    payload = json.dumps({"tool_name": "Grep", "tool_input": {"pattern": "zzz_nomatch_zzz"},
                          "session_id": "cold", "cwd": repo})
    times = []
    for _ in range(reps):
        t = time.perf_counter()
        subprocess.run(["uv", "run", "python", "-m", "auryn.hooks.postuse_enrich"],
                       input=payload, capture_output=True, text=True, env=env)
        times.append((time.perf_counter() - t) * 1000)
    return {"reps": reps, "p50_ms": round(_pct(times, 50), 1), "max_ms": round(max(times), 1)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--graph", required=True, help="graph name to query")
    ap.add_argument("--repo", required=True, help="repo root (for Read path resolution)")
    ap.add_argument("--from-transcripts", nargs="*", default=[], help="dirs of A/B run logs to harvest")
    ap.add_argument("--sample-from-graph", type=int, default=0, help="synthesise N symbol + N file events")
    ap.add_argument("--no-dedup", action="store_true", help="measure raw per-call fire rate (default)")
    ap.add_argument("--dedup", action="store_true", help="simulate one session's dedup savings")
    ap.add_argument("--measure-coldstart", action="store_true", help="also time the real per-process cost")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "results"))
    args = ap.parse_args()

    cfg = hook.HookConfig(graph_name=args.graph, dedup=args.dedup, metrics_path="/dev/null")
    ctx = hook.GraphContext(cfg)

    events: list[dict] = []
    if args.from_transcripts:
        events += harvest_transcripts(args.from_transcripts, args.repo)
    if args.sample_from_graph:
        events += sample_from_graph(ctx, args.repo, args.sample_from_graph)
    if not events:
        print("no events harvested -- pass --from-transcripts and/or --sample-from-graph")
        return

    report = replay(events, cfg)
    if args.measure_coldstart:
        report["coldstart"] = measure_coldstart(args.graph, args.repo)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "overhead.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    print(f"\nWrote {out_dir / 'overhead.json'}")


if __name__ == "__main__":
    main()
