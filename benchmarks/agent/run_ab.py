"""A/B benchmark: does having a code graph available make a coding agent cheaper, faster,
and more accurate?

Runs the SAME agent (headless `claude -p`), fully UNRESTRICTED -- it uses whatever tools it
wants (Read/Grep/Glob/Bash/...) -- on the SAME graded questions under two arms. The ONLY
difference is whether a code graph is available:

  A (no code graph): a normal Claude Code agent in the repo. No auryn MCP
                     (`--strict-mcp-config`), no code graph priming.
  B (code graph):    the same agent, PLUS the auryn MCP server (`--mcp-config`) and the
                     AGENTS.md + skill priming injected via `--append-system-prompt`.

Everything else is held constant (same model, turn cap, repo, isolation). We are NOT
testing "graph vs files" -- the agent may read/grep/glob freely in both arms; we're testing
whether *having the graph available* helps.

Clean slate: each run is a fresh, stateless `claude -p` invocation; `--strict-mcp-config`
means neither arm inherits any ambient MCP server; there's no global CLAUDE.md. (Auth lives
in the OS keychain, so we do NOT isolate the config dir -- that would log the agent out.)

Methodology adopted from Graphify's BENCHMARKS.md: fixed agent, capped turns,
key-fact-coverage grading with a citing judge, and a per-run spend ledger. We add time and
tool-mix, and capture the full stream-json event timeline for the run viewer.

Usage:
    # pilot: 3 tasks x 2 arms x 1 rep
    uv run python benchmarks/agent/run_ab.py --repo <path-to-falkordb-py> --limit 3
    # full:
    uv run python benchmarks/agent/run_ab.py --repo <path> --reps 3

Prereqs: FalkorDB up on localhost:6379; `claude` CLI on PATH; the target repo cloned; the
auryn installed here (the MCP server runs via `uv run --directory <this repo> auryn mcp`).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_AURYN_REPO = _HERE.parents[1]  # benchmarks/agent -> repo root
_PRIMING = _HERE / "priming"


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


# -- setup ---------------------------------------------------------------------


def ensure_indexed(repo: Path, graph_name: str, project_name: str) -> dict:
    """Index the target repo into `graph_name` if it isn't already (idempotent). Returns
    a one-time cost record (wall time; agent tokens are 0 -- indexing is our tool, not the
    agent). Graphify's model too: the graph is pre-built, the agent only queries."""
    from auryn.core import graph_queries as gq
    from auryn.core import ingest
    from auryn.core.graph_client import FalkorDBConfig, GraphWriter, connect_client

    client = connect_client(FalkorDBConfig(host="localhost", port=6379))
    try:
        client.connection.config_set("stop-writes-on-bgsave-error", "no")
    except Exception:
        pass
    graph = client.select_graph(graph_name)
    status = gq.graph_status(graph)
    if status["has_data"]:
        return {"already_indexed": True, "node_count": status["node_count"], "seconds": 0.0}
    started = time.monotonic()
    report = ingest.ingest_directory(GraphWriter(graph), str(repo), project_name=project_name)
    elapsed = time.monotonic() - started
    totals = report.to_dict()["totals"]
    return {"already_indexed": False, "nodes_added": totals["nodes_added"], "seconds": round(elapsed, 1)}


def build_mcp_config(graph_name: str, out_dir: Path) -> Path:
    """A .mcp.json launching our stdio MCP server against `graph_name`, run from the
    auryn repo so `auryn` resolves."""
    config = {
        "mcpServers": {
            "auryn": {
                "command": "uv",
                "args": ["run", "--directory", str(_AURYN_REPO), "auryn", "mcp", "--graph-name", graph_name],
            }
        }
    }
    path = out_dir / "mcp.json"
    path.write_text(json.dumps(config, indent=2))
    return path


def build_priming() -> str:
    """The Arm-B system-prompt injection: a CONCISE, tool-selecting directive (kept short so
    it doesn't inflate per-turn context). It steers transitive/closure/impact questions to the
    ONE-SHOT tools -- the biggest inefficiency in round 1 was the agent walking the call graph
    hop-by-hop with many small queries instead of a single behavioral_inventory/impact_of."""
    return (
        "This repository has a queryable code graph via the `auryn` MCP server (tools named "
        "`mcp__auryn__*`). For ANY question about code structure or relationships, use the "
        "graph INSTEAD OF grepping/reading files:\n"
        "- start: mcp__auryn__auryn_status, then mcp__auryn__locate(name) to get a node id;\n"
        "- 'who calls X / what uses X': mcp__auryn__who_calls (pass max_depth>1 for TRANSITIVE callers) -- one call, not many;\n"
        "- 'what does X call / trace X end to end / X's full behavior': mcp__auryn__behavioral_inventory(id) -- ONE call returns the ENTIRE transitive call-closure; do NOT walk it hop by hop;\n"
        "- 'everything affected if X changes': mcp__auryn__impact_of(id, direction='upstream', max_depth=N);\n"
        "- 'does X reach Y': mcp__auryn__find_path(a, b) (any edge type; edge_types=['CALLS'] for a call chain); subclasses/inheritance or anything custom: mcp__auryn__get_schema then mcp__auryn__run_cypher (e.g. MATCH (c:Class)-[:EXTENDS*1..]->(b:Class {name:'...'}) RETURN c.qualified_name).\n"
        "Prefer ONE closure/impact/who_calls(max_depth) call over many small queries. Only read files "
        "if the graph genuinely lacks the answer, and cite the ids/paths it returns as evidence."
    )


def build_hook_settings(graph_name: str, out_dir: Path, metrics_path: Path) -> Path:
    """A settings.json enabling the PostToolUse enrichment hook on the reflex tools (arms C/D).

    The hook runs the repo's venv python directly (not `uv run`, which adds resolution overhead to
    every single tool call) and is handed the graph name + a shared metrics log via inline env; we
    attribute a run's hook activity afterwards by the session_id the hook records. No priming, no
    MCP -- arm C is *pure* injection with zero behaviour change asked of the agent."""
    venv_py = _AURYN_REPO / ".venv" / "bin" / "python"
    python = str(venv_py) if venv_py.exists() else "python3"
    command = (
        f"AURYN_HOOK_GRAPH={graph_name} AURYN_HOOK_METRICS={metrics_path} "
        f"FALKORDB_HOST=localhost FALKORDB_PORT=6379 "
        f"{python} -m auryn.hooks.postuse_enrich"
    )
    settings = {
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": "Grep|Read|Bash|Edit|Write|MultiEdit",
                    "hooks": [{"type": "command", "command": command, "timeout": 15}],
                }
            ]
        }
    }
    path = out_dir / "hook-settings.json"
    path.write_text(json.dumps(settings, indent=2))
    return path


def read_hook_metrics(metrics_path: Path, session_id: str | None) -> dict:
    """Aggregate the hook's self-recorded metrics for one run (matched by session_id): fire count,
    suppression breakdown, injected-token total, per-fire latency, and the set of injected symbol
    names (so influence can be measured against the transcript)."""
    empty = {"fires": 0, "suppressed": 0, "injected_tokens": 0, "reasons": {}, "symbols": [],
             "latency_p50_ms": 0.0, "latency_p95_ms": 0.0}
    if not session_id or not metrics_path.exists():
        return empty
    fires = suppressed = tokens = 0
    reasons: dict[str, int] = {}
    symbols: list[str] = []
    latencies: list[float] = []
    for line in metrics_path.read_text().splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("session") != session_id:
            continue
        latencies.append(rec.get("latency_ms", 0.0))
        reasons[rec.get("reason", "?")] = reasons.get(rec.get("reason", "?"), 0) + 1
        if rec.get("fired"):
            fires += 1
            tokens += rec.get("injected_tokens", 0)
            symbols.extend(rec.get("symbols") or [])
        else:
            suppressed += 1
    latencies.sort()

    def pct(p):
        return round(latencies[min(len(latencies) - 1, int(round(p / 100 * (len(latencies) - 1))))], 1) if latencies else 0.0

    return {"fires": fires, "suppressed": suppressed, "injected_tokens": tokens,
            "reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
            "symbols": sorted(set(s for s in symbols if s)),
            "latency_p50_ms": pct(50), "latency_p95_ms": pct(95)}


def measure_influence(injected_symbols: list[str], timeline: list[dict], answer: str) -> dict:
    """Did the injected context actually get *used*? A fired hint is pure waste if the agent never
    acts on it. Heuristic: an injected symbol name that later appears in a tool-call input or in
    the final answer counts as 'referenced'. Reports how many of the injected symbols were."""
    if not injected_symbols:
        return {"injected": 0, "referenced": 0, "rate": None}
    hay_parts: list[str] = [answer or ""]
    for ev in timeline:
        if ev.get("kind") == "tool_use":
            hay_parts.append(json.dumps(ev.get("input") or {}))
    hay = "\n".join(hay_parts)
    referenced = sum(1 for s in set(injected_symbols) if s and s in hay)
    uniq = len(set(injected_symbols))
    return {"injected": uniq, "referenced": referenced, "rate": round(referenced / uniq, 2) if uniq else None}


# -- one agent run -------------------------------------------------------------


def parse_stream(stdout: str) -> dict:
    """Parse `claude --output-format stream-json` (newline-delimited JSON) into a distilled
    timeline (thinking / tool_use / tool_result / text) plus the final result envelope."""
    timeline: list[dict] = []
    result_event: dict | None = None
    tool_calls: list[dict] = []
    init: dict = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = ev.get("type")
        if etype == "system" and ev.get("subtype") == "init":
            # Confirms whether the auryn MCP actually connected and which tools the
            # agent had available -- so "0 mcp tools used" can't be confused with "mcp
            # never connected".
            init = {
                "mcp_servers": ev.get("mcp_servers", []),
                "auryn_tools": [t for t in ev.get("tools", []) if "auryn" in str(t)],
                "all_tools": ev.get("tools", []),
            }
        elif etype == "assistant":
            for block in ev.get("message", {}).get("content", []):
                bt = block.get("type")
                if bt == "thinking":
                    timeline.append({"kind": "thinking", "text": block.get("thinking", "")})
                elif bt == "text":
                    timeline.append({"kind": "text", "text": block.get("text", "")})
                elif bt == "tool_use":
                    tc = {"kind": "tool_use", "id": block.get("id"), "name": block.get("name"), "input": block.get("input")}
                    timeline.append(tc)
                    tool_calls.append({"name": block.get("name"), "input": block.get("input")})
        elif etype == "user":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "tool_result":
                    content = block.get("content")
                    text = content if isinstance(content, str) else json.dumps(content)[:4000]
                    timeline.append({"kind": "tool_result", "tool_use_id": block.get("tool_use_id"), "text": text[:4000]})
        elif etype == "result":
            result_event = ev
    return {"timeline": timeline, "tool_calls": tool_calls, "result": result_event, "init": init}


def classify_tools(tool_calls: list[dict]) -> dict:
    """Split tool usage into code graph (MCP) vs file tools vs other -- the "less searching,
    more finding" signal."""
    mcp, files, other = 0, 0, 0
    for tc in tool_calls:
        name = (tc.get("name") or "")
        if name.startswith("mcp__auryn__") or name.startswith("auryn"):
            mcp += 1
        elif name in ("Read", "Grep", "Glob", "Bash", "LS"):
            files += 1
        else:
            other += 1
    return {"mcp": mcp, "file": files, "other": other, "total": len(tool_calls)}


def run_agent(task: dict, arm: str, repo: Path, mcp_config: Path, priming: str, model: str,
              max_turns: int, timeout: int, hook_settings: Path | None = None,
              hook_metrics: Path | None = None) -> dict:
    """Run one arm:
      A -- bare agent (no graph).
      B -- MCP graph available + priming (must be adopted).
      C -- PostToolUse hook enriching Grep/Read/Bash/Edit (no MCP, no priming): pure injection.
      D -- hook + MCP + priming together (does injection also lift adoption?).
    """
    cmd = [
        "claude", "-p", task["question"],
        "--output-format", "stream-json", "--verbose",
        "--model", model,
        "--max-turns", str(max_turns),
        "--permission-mode", "bypassPermissions",
        "--strict-mcp-config",
        "--add-dir", str(repo),
    ]
    if arm in ("B", "D"):
        cmd += ["--mcp-config", str(mcp_config), "--append-system-prompt", priming]
    if arm in ("C", "D") and hook_settings is not None:
        cmd += ["--settings", str(hook_settings)]

    # Inherit the real env (auth is in the OS keychain / default config dir); clean-slate
    # comes from the stateless -p session + --strict-mcp-config, not config-dir isolation.
    started = time.monotonic()
    try:
        proc = _run(cmd, cwd=str(repo), timeout=timeout)
        stdout, stderr, rc = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as e:
        stdout, stderr, rc = (e.stdout or ""), "TIMEOUT", 124
    wall = round(time.monotonic() - started, 1)

    parsed = parse_stream(stdout if isinstance(stdout, str) else stdout.decode("utf-8", "replace"))
    result = parsed["result"] or {}
    usage = result.get("usage", {})
    init = parsed["init"]
    session_id = result.get("session_id")

    # arms C/D: attribute the hook's self-recorded activity to this run + measure influence
    hook_stats: dict = {}
    influence: dict = {}
    if arm in ("C", "D") and hook_metrics is not None:
        hook_stats = read_hook_metrics(hook_metrics, session_id)
        influence = measure_influence(hook_stats.get("symbols", []), parsed["timeline"], result.get("result", ""))
    # The server is listed at init with status "pending" and connects lazily on first use,
    # so "present and not failed" == available (confirmed: agents use it fine from pending).
    mcp_connected = any(
        str(s.get("name")) == "auryn" and str(s.get("status")).lower() not in ("failed", "error")
        for s in init.get("mcp_servers", [])
    )
    return {
        "arm": arm,
        "task_id": task["id"],
        "returncode": rc,
        "wall_seconds": wall,
        "mcp_connected": mcp_connected,
        "auryn_tools_available": len(init.get("auryn_tools", [])),
        "mcp_servers": init.get("mcp_servers", []),
        "answer": result.get("result", ""),
        "cost_usd": result.get("total_cost_usd"),
        "duration_ms": result.get("duration_ms"),
        "num_turns": result.get("num_turns"),
        "tokens": {
            "input": usage.get("input_tokens", 0),
            "output": usage.get("output_tokens", 0),
            "cache_creation": usage.get("cache_creation_input_tokens", 0),
            "cache_read": usage.get("cache_read_input_tokens", 0),
            "total": usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0) + usage.get("cache_read_input_tokens", 0),
        },
        "tool_mix": classify_tools(parsed["tool_calls"]),
        "session_id": session_id,
        "hook": hook_stats,
        "hook_influence": influence,
        "timeline": parsed["timeline"],
        "stderr_tail": (stderr or "")[-500:],
    }


# -- grading -------------------------------------------------------------------

_JUDGE_PROMPT = """You are grading a coding agent's answer against a fixed gold set of key facts.
For EACH gold fact, decide: "covered" (the answer clearly states it), "partial" (gestures at
it but incomplete/imprecise), or "missing". Cite a short verbatim quote from the answer for
covered/partial. Output ONLY a JSON object, no prose:

{{"facts": [{{"fact": "...", "verdict": "covered|partial|missing", "quote": "..."}}], "coverage": 0.0}}

coverage = (num covered + 0.5 * num partial) / total facts, rounded to 2 decimals.

QUESTION:
{question}

GOLD FACTS:
{facts}

AGENT ANSWER:
{answer}
"""


def grade(task: dict, answer: str, judge_model: str, timeout: int) -> dict:
    if not answer.strip():
        return {"coverage": 0.0, "facts": [], "error": "empty answer"}
    prompt = _JUDGE_PROMPT.format(
        question=task["question"],
        facts="\n".join(f"- {f}" for f in task["gold_facts"]),
        answer=answer,
    )
    proc = _run(
        ["claude", "-p", prompt, "--output-format", "json", "--model", judge_model, "--max-turns", "1", "--strict-mcp-config"],
        timeout=timeout,
    )
    try:
        envelope = json.loads(proc.stdout)
        verdict = json.loads(envelope.get("result", "{}"))
        # trust our own arithmetic over the judge's self-reported number
        facts = verdict.get("facts", [])
        covered = sum(1 for f in facts if f.get("verdict") == "covered")
        partial = sum(1 for f in facts if f.get("verdict") == "partial")
        total = len(task["gold_facts"]) or 1
        verdict["coverage"] = round((covered + 0.5 * partial) / total, 2)
        return verdict
    except Exception as e:
        return {"coverage": None, "facts": [], "error": f"judge parse failed: {e}", "raw": proc.stdout[-500:]}


# -- orchestration -------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="path to the (cloned) target repo")
    ap.add_argument("--tasks", default=str(_HERE / "tasks.json"))
    ap.add_argument("--arms", default="A,B,C,D",
                    help="A=bare, B=MCP+priming, C=hook injection, D=hook+MCP")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--limit", type=int, default=None, help="only the first N tasks (pilot)")
    ap.add_argument("--model", default="sonnet", help="agent model (alias or id); opus is more representative but pricier")
    ap.add_argument("--judge-model", default="sonnet")
    ap.add_argument("--max-turns", type=int, default=14)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--out", default=str(_HERE / "results"))
    ap.add_argument("--dry-run", action="store_true", help="print the plan and the first command, don't run agents")
    args = ap.parse_args()

    spec = json.loads(Path(args.tasks).read_text())
    tasks = spec["tasks"][: args.limit] if args.limit else spec["tasks"]
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    repo = Path(args.repo).resolve()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    # absolute -- the agent runs with cwd=repo, so config paths (mcp.json / hook-settings.json)
    # handed to `claude` must not be relative to this process's cwd.
    out_dir = (Path(args.out) / stamp).resolve()
    (out_dir / "runs").mkdir(parents=True, exist_ok=True)

    index_cost = ensure_indexed(repo, spec["graph_name"], spec["project_name"])
    mcp_config = build_mcp_config(spec["graph_name"], out_dir)
    priming = build_priming()
    hook_metrics = out_dir / "hook-metrics.jsonl"
    hook_settings = build_hook_settings(spec["graph_name"], out_dir, hook_metrics)

    plan = [(t, arm, rep) for t in tasks for arm in arms for rep in range(args.reps)]
    print(f"target repo:   {repo}")
    print(f"index (one-time): {index_cost}")
    print(f"plan:          {len(tasks)} tasks x {len(arms)} arms x {args.reps} rep(s) = {len(plan)} runs")
    print(f"model:         {args.model}  (judge: {args.judge_model}, max_turns: {args.max_turns})")
    print(f"out:           {out_dir}")

    if args.dry_run:
        t, arm, _ = plan[0]
        print("\n[dry-run] first run:", t["id"], "arm", arm)
        return

    runs = []
    for i, (task, arm, rep) in enumerate(plan, 1):
        print(f"\n[{i}/{len(plan)}] {task['id']} arm={arm} rep={rep} ...", flush=True)
        run = run_agent(task, arm, repo, mcp_config, priming, args.model, args.max_turns, args.timeout,
                        hook_settings=hook_settings, hook_metrics=hook_metrics)
        run["rep"] = rep
        run["question"] = task["question"]
        run["gold_facts"] = task["gold_facts"]
        run["grade"] = grade(task, run["answer"], args.judge_model, args.timeout)
        runs.append(run)
        cov = run["grade"].get("coverage")
        mcp_note = f" mcp_connected={run['mcp_connected']}(tools={run['auryn_tools_available']})" if arm in ("B", "D") else ""
        hook_note = ""
        if arm in ("C", "D") and run.get("hook"):
            h = run["hook"]
            infl = run.get("hook_influence", {}).get("rate")
            hook_note = f" hook_fires={h.get('fires', 0)} inj_tok={h.get('injected_tokens', 0)} influence={infl}"
        print(f"    tokens={run['tokens']['total']:,} cost=${run['cost_usd']} time={run['wall_seconds']}s "
              f"turns={run['num_turns']} tools={run['tool_mix']} coverage={cov}{mcp_note}{hook_note}")
        (out_dir / "runs" / f"{task['id']}__{arm}__r{rep}.json").write_text(json.dumps(run, indent=2))

    summary = {
        "timestamp": stamp,
        "target_repo": spec["target_repo"],
        "graph_name": spec["graph_name"],
        "model": args.model,
        "judge_model": args.judge_model,
        "max_turns": args.max_turns,
        "index_cost": index_cost,
        "runs": [
            {k: r[k] for k in ("task_id", "arm", "rep", "tokens", "cost_usd", "wall_seconds", "num_turns", "tool_mix")}
            | {"coverage": r["grade"].get("coverage"), "hook": r.get("hook", {}),
               "hook_influence": r.get("hook_influence", {})}
            for r in runs
        ],
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print(f"\nWrote {len(runs)} run logs + summary.json to {out_dir}")
    _print_table(summary["runs"])


def _print_table(rows: list[dict]) -> None:
    # aggregate by arm
    by_arm: dict[str, list[dict]] = {}
    for r in rows:
        by_arm.setdefault(r["arm"], []).append(r)
    print("\n=== A/B/C/D means (across all runs) ===")
    print(f"{'arm':<4} {'tokens':>10} {'cost$':>8} {'time_s':>7} {'turns':>6} {'mcp/file':>9} "
          f"{'coverage':>9} {'fires':>6} {'inj_tok':>8} {'infl':>6}")
    for arm, rs in sorted(by_arm.items()):
        n = len(rs)
        tok = sum(r["tokens"]["total"] for r in rs) / n
        cost = sum((r["cost_usd"] or 0) for r in rs) / n
        tm = sum(r["wall_seconds"] for r in rs) / n
        turns = sum((r["num_turns"] or 0) for r in rs) / n
        mcp = sum(r["tool_mix"]["mcp"] for r in rs) / n
        fil = sum(r["tool_mix"]["file"] for r in rs) / n
        cov = [r["coverage"] for r in rs if r["coverage"] is not None]
        covm = sum(cov) / len(cov) if cov else float("nan")
        fires = sum((r.get("hook") or {}).get("fires", 0) for r in rs) / n
        inj = sum((r.get("hook") or {}).get("injected_tokens", 0) for r in rs) / n
        infl_vals = [r.get("hook_influence", {}).get("rate") for r in rs]
        infl_vals = [v for v in infl_vals if v is not None]
        infl = sum(infl_vals) / len(infl_vals) if infl_vals else float("nan")
        print(f"{arm:<4} {tok:>10,.0f} {cost:>8.3f} {tm:>7.1f} {turns:>6.1f} {mcp:>4.1f}/{fil:<4.1f} "
              f"{covm:>9.2f} {fires:>6.1f} {inj:>8,.0f} {infl:>6.2f}")


if __name__ == "__main__":
    main()
