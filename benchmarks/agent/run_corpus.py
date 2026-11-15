"""Code-generation A/B on an UNFAMILIAR codebase (the `claimflow` corpus) -- the setting the
Django benchmark can't test, because Django is in the model's training data so the agent
navigates from memory and never adopts the graph. On code the model has never seen, a
graph-equipped repo (documented in its CLAUDE.md) actually gets used (validated), so we can
measure whether the graph makes code-gen cheaper/faster/more accurate.

Per (task x arm):
  1. fresh copy of the corpus (a git repo, for clean diffs).
  2. arm B: index it into a code graph + drop a CLAUDE.md documenting the graph (the adoption
     lever; read at session start, not per-turn prompt bloat -> token axis stays fair).
  3. run the agent (`claude -p`, unrestricted tools) with ONLY the issue text -- no tests,
     no network (web tools disallowed; network Bash flagged).
  4. GRADE objectively: copy the task's hidden acceptance test in and run it with pytest.
     The test fails on the base, so passing == the agent's change is correct.

Arms: A = plain repo; B = repo + auryn MCP + CLAUDE.md. Prompt identical in both.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from run_ab import classify_tools, parse_stream
from run_swe import _delete_graph, _mcp_config_for, _network_bash

_HERE = Path(__file__).resolve().parent

_CORPUS_CLAUDE_MD = (
    "# claimflow\n\n"
    "This repository is indexed as a queryable **code graph** via the `auryn` MCP server "
    "-- the intended way to navigate this codebase. Adjudication *stages* and policy *rules* "
    "register by name/category and are dispatched dynamically (see core/registry.py and "
    "rules/registry.py), so grepping for \"what calls or applies X\" is unreliable here; the "
    "graph resolves the call structure and impact precisely.\n\n"
    "The auryn tools are DEFERRED -- load them once at the start with:\n"
    "  ToolSearch  select:mcp__auryn__auryn_status,mcp__auryn__locate,"
    "mcp__auryn__who_calls,mcp__auryn__callees,mcp__auryn__impact_of,"
    "mcp__auryn__get_context,mcp__auryn__behavioral_inventory,"
    "mcp__auryn__find_path,mcp__auryn__get_schema,mcp__auryn__run_cypher\n\n"
    "Then: `locate(name)` -> id; `who_calls(id)` / `impact_of(id)` to find EVERY caller/site "
    "before changing shared code; `find_path(a, b)` to trace how one function reaches another "
    "through the dispatchers; `get_context(id)` to read a symbol. Prefer the graph over grep "
    "for structure and impact.\n"
)


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def build_prompt(issue: str) -> str:
    return (
        "You are working in a Python project: a claims-adjudication engine. Resolve the "
        "following issue by EDITING THE SOURCE CODE (make the actual changes with your editing "
        "tools -- do not just describe the fix). You have NO network access; work only from the "
        "code in this repository. Match the codebase's existing conventions.\n\n"
        "<issue>\n" + issue.strip() + "\n</issue>\n\n"
        "When finished, briefly summarize the files and functions you changed and why."
    )


def index_corpus(worktree: Path, graph_name: str) -> dict:
    from auryn.core import ingest
    from auryn.core.graph_client import FalkorDBConfig, GraphWriter, connect_client

    client = connect_client(FalkorDBConfig(host="localhost", port=6379))
    try:
        client.connection.config_set("stop-writes-on-bgsave-error", "no")
    except Exception:
        pass
    graph = client.select_graph(graph_name)
    try:
        graph.delete()
    except Exception:
        pass
    report = ingest.ingest_directory(GraphWriter(graph), str(worktree), project_name="claimflow",
                                     deep="off", auto_install=False)
    py = next((r for r in report.languages if r.language == "python"), None)
    return {"files": py.files_detected if py else 0, "nodes_added": (py.result.nodes_added if py and py.result else 0)}


def run_agent(issue: str, arm: str, worktree: Path, mcp_config: Path | None,
              model: str, max_turns: int, timeout: int) -> dict:
    cmd = [
        "claude", "-p", build_prompt(issue),
        "--output-format", "stream-json", "--verbose",
        "--model", model, "--max-turns", str(max_turns),
        "--permission-mode", "bypassPermissions",
        "--strict-mcp-config",
        "--disallowedTools", "WebSearch", "WebFetch",
        "--add-dir", str(worktree),
    ]
    if arm == "B":
        cmd += ["--mcp-config", str(mcp_config)]  # adoption via the repo CLAUDE.md in the worktree

    started = time.monotonic()
    try:
        proc = _run(cmd, cwd=str(worktree), timeout=timeout)
        stdout, stderr, rc = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as e:
        stdout, stderr, rc = (e.stdout or ""), "TIMEOUT", 124

    wall = round(time.monotonic() - started, 1)
    parsed = parse_stream(stdout if isinstance(stdout, str) else stdout.decode("utf-8", "replace"))
    result = parsed["result"] or {}
    usage = result.get("usage", {})
    graph_calls = sum(1 for t in parsed["timeline"]
                      if t.get("kind") == "tool_use" and (t["name"] == "ToolSearch" or "auryn" in t["name"]))
    return {
        "arm": arm, "returncode": rc, "wall_seconds": wall,
        "answer": result.get("result", ""), "cost_usd": result.get("total_cost_usd"),
        "num_turns": result.get("num_turns"),
        "tokens": {
            "input": usage.get("input_tokens", 0), "output": usage.get("output_tokens", 0),
            "cache_creation": usage.get("cache_creation_input_tokens", 0),
            "cache_read": usage.get("cache_read_input_tokens", 0),
            "total": usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0) + usage.get("cache_read_input_tokens", 0),
        },
        "tool_mix": classify_tools(parsed["tool_calls"]),
        "graph_calls": graph_calls,
        "network_bash_flags": _network_bash(parsed["tool_calls"]),
        "timeline": parsed["timeline"],
        "stderr_tail": (stderr or "")[-400:],
    }


def capture_patch(worktree: Path) -> dict:
    diff = _run(["git", "-C", str(worktree), "diff"]).stdout
    files = sorted({l[6:] for l in diff.splitlines() if l.startswith("+++ b/")})
    # new untracked files the agent added (e.g. a new stage module)
    untracked = [l for l in _run(["git", "-C", str(worktree), "status", "--porcelain"]).stdout.splitlines()
                 if l.startswith("??") and l.strip().endswith(".py")]
    return {"diff": diff, "files_changed": files, "n_files": len(files) + len(untracked),
            "new_files": [u[3:] for u in untracked]}


def grade(worktree: Path, hidden_test: Path, timeout: int) -> dict:
    dest = worktree / "tests" / hidden_test.name
    shutil.copy(hidden_test, dest)
    try:
        env = {**os.environ, "PYTHONPATH": str(worktree)}
        proc = _run(["uv", "run", "--no-project", "--with", "pytest", "pytest",
                     f"tests/{hidden_test.name}", "-q"], cwd=str(worktree), timeout=timeout, env=env)
        out = proc.stdout + "\n" + proc.stderr
        resolved = proc.returncode == 0 and " passed" in out
        return {"resolved": bool(resolved), "returncode": proc.returncode, "output_tail": out[-800:]}
    finally:
        dest.unlink(missing_ok=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True, help="corpus dir containing base/, hidden/, tasks.json")
    ap.add_argument("--work-root", required=True)
    ap.add_argument("--arms", default="A,B")
    ap.add_argument("--tasks", default=None, help="comma list of task ids; default all")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--max-turns", type=int, default=40)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--graph-name", default="corpus_cf")
    ap.add_argument("--out", default=str(_HERE / "results-corpus"))
    args = ap.parse_args()

    os.environ["AURYN_AUTO_INSTALL"] = "0"
    corpus = Path(args.corpus).resolve()
    base = corpus / "base"
    tasks = json.loads((corpus / "tasks.json").read_text())
    if args.tasks:
        want = {t.strip() for t in args.tasks.split(",")}
        tasks = [t for t in tasks if t["id"] in want]
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    work_root = Path(args.work_root).resolve()
    work_root.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out) / stamp
    (out_dir / "runs").mkdir(parents=True, exist_ok=True)

    plan = [(t, arm, rep) for t in tasks for arm in arms for rep in range(args.reps)]
    print(f"corpus: {corpus}\ntasks: {[t['id'] for t in tasks]}\narms: {arms} reps: {args.reps}\n"
          f"model: {args.model}  out: {out_dir}")

    runs = []
    for i, (task, arm, rep) in enumerate(plan, 1):
        tid = task["id"]
        print(f"\n[{i}/{len(plan)}] {tid} arm={arm} rep={rep} ...", flush=True)
        wt = work_root / f"{tid}__{arm}__r{rep}"
        if wt.exists():
            shutil.rmtree(wt)
        shutil.copytree(base, wt)
        graph_name = f"{args.graph_name}_{tid}"
        mcp_config = None
        index_info = None
        try:
            if arm == "B":
                index_info = index_corpus(wt, graph_name)
                mcp_config = _mcp_config_for(graph_name, out_dir, f"{tid}_{rep}")
                (wt / "CLAUDE.md").write_text(_CORPUS_CLAUDE_MD)
                print(f"    indexed: {index_info} (graph {graph_name})")
            run = run_agent(task["issue"], arm, wt, mcp_config, args.model, args.max_turns, args.timeout)
            run["model_patch"] = capture_patch(wt)
            run["grade"] = grade(wt, corpus / "hidden" / task["hidden_test"], 120)
        finally:
            if arm == "B":
                _delete_graph(graph_name)
            shutil.rmtree(wt, ignore_errors=True)
        run["task_id"] = tid
        run["rep"] = rep
        run["index"] = index_info
        runs.append(run)
        g = run["grade"]
        print(f"    resolved={g.get('resolved')} tokens={run['tokens']['total']:,} "
              f"cost=${run['cost_usd']} time={run['wall_seconds']}s turns={run['num_turns']} "
              f"tools={run['tool_mix']} graph_calls={run['graph_calls']} "
              f"files={run['model_patch']['n_files']} net={len(run['network_bash_flags'])}")
        (out_dir / "runs" / f"{tid}__{arm}__r{rep}.json").write_text(json.dumps(run, indent=2))

    _summary(runs, out_dir, args)
    _table(runs)


def _summary(runs, out_dir, args):
    (out_dir / "summary.json").write_text(json.dumps({
        "timestamp": out_dir.name, "benchmark": "claimflow-unfamiliar-codegen",
        "model": args.model,
        "runs": [{k: r[k] for k in ("task_id", "arm", "rep", "tokens", "cost_usd", "wall_seconds",
                                    "num_turns", "tool_mix", "graph_calls", "network_bash_flags")}
                 | {"resolved": r["grade"].get("resolved"), "files_changed": r["model_patch"]["files_changed"],
                    "new_files": r["model_patch"]["new_files"]}
                 for r in runs],
    }, indent=2))
    print(f"\nWrote {len(runs)} runs + summary.json to {out_dir}")


def _table(runs):
    by_arm: dict[str, list] = {}
    for r in runs:
        by_arm.setdefault(r["arm"], []).append(r)
    print("\n=== A/B means (unfamiliar codebase; accuracy = hidden-test resolved) ===")
    print(f"{'arm':<4} {'resolved':>9} {'tokens':>10} {'cost$':>8} {'time_s':>7} {'turns':>6} {'graph':>6} {'mcp/file':>9}")
    for arm, rs in sorted(by_arm.items()):
        n = len(rs)
        res = sum(1 for r in rs if r["grade"].get("resolved")) / n
        tok = sum(r["tokens"]["total"] for r in rs) / n
        cost = sum((r["cost_usd"] or 0) for r in rs) / n
        tm = sum(r["wall_seconds"] for r in rs) / n
        turns = sum((r["num_turns"] or 0) for r in rs) / n
        gc = sum(r["graph_calls"] for r in rs) / n
        mcp = sum(r["tool_mix"]["mcp"] for r in rs) / n
        fil = sum(r["tool_mix"]["file"] for r in rs) / n
        print(f"{arm:<4} {res:>8.0%} {tok:>10,.0f} {cost:>8.3f} {tm:>7.1f} {turns:>6.1f} {gc:>6.1f} {mcp:>4.1f}/{fil:<4.1f}")


if __name__ == "__main__":
    main()
