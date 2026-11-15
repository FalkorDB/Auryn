"""Code-generation A/B benchmark: does a code graph make an agent better at *writing* code
(not just answering questions)? Same design as run_ab.py, but the task is to RESOLVE a real
Django issue by editing the repo, and accuracy is graded OBJECTIVELY by the issue's own tests
(SWE-bench Verified) -- no judge.

Per (instance x arm):
  1. `git worktree` of Django at the issue's parent commit (pre-fix state).
  2. arm B only: index that worktree into a code graph (fast ast engine).
  3. run the agent (`claude -p`, unrestricted tools) with ONLY the issue text -- no issue id,
     no hint that a PR exists, and NO network (WebSearch/WebFetch disallowed; any Bash network
     use is flagged). The agent never sees the tests.
  4. GRADE: apply the PR's *test* patch onto the agent's edits and run the affected test
     modules (`runtests.py --parallel 1`). Resolved == those modules pass. The FAIL_TO_PASS
     tests fail on the unfixed code, so this is a real pass/fail of the agent's fix.

Arms: A = repo only (no MCP); B = repo + auryn MCP + code-gen priming. Both no network.
Cheat control: the gold *code* patch is never shown or on disk; the tests aren't present
during the agent run; the issue id is withheld; network is off; traces are scanned for any
network Bash. Memorization (Django is in the model's weights) is constant across arms, so it
cancels in the B-A delta.

Usage:
    uv run python benchmarks/agent/run_swe.py \
        --config <scratch>/swe/config.json --patches-dir <scratch>/swe \
        --django-src <scratch>/django-src --test-python <scratch>/djvenv/bin/python \
        --model sonnet
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from run_ab import classify_tools, parse_stream  # same-dir reuse

_HERE = Path(__file__).resolve().parent
_AURYN_REPO = _HERE.parents[1]

# Bash tokens that would mean the agent tried to LOOK UP the public fix (fetch a URL, clone,
# query GitHub). Flagged, not blocked -- with WebSearch/WebFetch already disallowed this is a
# tripwire for the writeup. Deliberately NOT flagging `pip`/`uv` (installing a dependency to
# run code locally is problem-solving, not looking up the solution).
_NET_PAT = re.compile(r"(\bcurl\b|\bwget\b|\bgh\b|git\s+(fetch|pull|clone)|https?://)")


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


# -- worktree + env ------------------------------------------------------------

def prepare_worktree(django_src: Path, base_commit: str, dest: Path) -> None:
    _run(["git", "-C", str(django_src), "worktree", "remove", "--force", str(dest)])
    r = _run(["git", "-C", str(django_src), "worktree", "add", "--detach", str(dest), base_commit])
    if r.returncode != 0:
        raise RuntimeError(f"worktree add failed for {base_commit}: {r.stderr[-400:]}")


def remove_worktree(django_src: Path, dest: Path) -> None:
    _run(["git", "-C", str(django_src), "worktree", "remove", "--force", str(dest)])


# The adoption lever (validated): a graph-equipped repo DOCUMENTS its graph in the standard
# instructions file the agent reads at session start -- exactly how a real project ships
# AGENTS.md. This is what makes the agent actually USE the graph for code-gen (a per-turn
# --append-system-prompt note was read but ignored). It's a one-time context read, not
# per-turn prompt bloat, so the token comparison stays fair; it's part of "the repo has a
# graph." Written into arm B's worktree only; arm A is a plain repo.
_GRAPH_CLAUDE_MD = (
    "# Django\n\n"
    "This repository is indexed as a queryable **code graph**, exposed by the `auryn` MCP "
    "server -- the intended way to navigate this large codebase (grep across ~2700 files is "
    "noisy; the graph resolves structure and impact precisely).\n\n"
    "The auryn tools are DEFERRED -- load them once at the start with:\n"
    "  ToolSearch  select:mcp__auryn__auryn_status,mcp__auryn__locate,"
    "mcp__auryn__who_calls,mcp__auryn__callees,mcp__auryn__impact_of,"
    "mcp__auryn__get_context,mcp__auryn__behavioral_inventory,"
    "mcp__auryn__find_path,mcp__auryn__get_schema,mcp__auryn__run_cypher\n\n"
    "Then: `locate(name)` -> id; `who_calls(id)` / `impact_of(id)` to find EVERY caller of a "
    "shared symbol before you change it (a fix to shared code must satisfy all its callers); "
    "`behavioral_inventory(id)` for a symbol's full behavior; `get_context(id)` to read a "
    "symbol. Prefer the graph over grep for structure and impact.\n"
)


def _write_graph_claude_md(worktree: Path) -> None:
    (worktree / "CLAUDE.md").write_text(_GRAPH_CLAUDE_MD)


def _mcp_config_for(graph_name: str, out_dir: Path, iid: str) -> Path:
    """A per-instance .mcp.json launching our stdio MCP server against this instance's own
    graph (run from the auryn repo so `auryn` resolves)."""
    cfg = {"mcpServers": {"auryn": {"command": "uv",
           "args": ["run", "--directory", str(_AURYN_REPO), "auryn", "mcp", "--graph-name", graph_name]}}}
    path = out_dir / f"mcp_{iid}.json"
    path.write_text(json.dumps(cfg, indent=2))
    return path


def _delete_graph(graph_name: str) -> None:
    try:
        from auryn.core.graph_client import FalkorDBConfig, connect_client
        connect_client(FalkorDBConfig(host="localhost", port=6379)).select_graph(graph_name).delete()
    except Exception:
        pass


def index_worktree(worktree: Path, graph_name: str) -> dict:
    from auryn.core import ingest
    from auryn.core.graph_client import FalkorDBConfig, GraphWriter, connect_client

    client = connect_client(FalkorDBConfig(host="localhost", port=6379))
    try:
        client.connection.config_set("stop-writes-on-bgsave-error", "no")
    except Exception:
        pass
    graph = client.select_graph(graph_name)
    try:
        graph.delete()  # fresh graph per instance (worktrees differ by commit)
    except Exception:
        pass
    started = time.monotonic()
    # deep="off": the fast ast graph (who-calls/impact/classes) is enough for localization and
    # indexes all of Django in ~10s; auto-install off so Django's .js files don't trigger a PyPI pull.
    report = ingest.ingest_directory(GraphWriter(graph), str(worktree), project_name="django",
                                     deep="off", auto_install=False)
    py = next((r for r in report.languages if r.language == "python"), None)
    return {"seconds": round(time.monotonic() - started, 1),
            "files": py.files_detected if py else 0,
            "nodes_added": (py.result.nodes_added if py and py.result else 0)}


# -- the agent run -------------------------------------------------------------


def build_prompt(problem_statement: str) -> str:
    # IDENTICAL in both arms (no issue id, no PR/tests). The only difference between arms is
    # arm B's repo having the auryn MCP + a CLAUDE.md documenting it -- the prompt itself
    # is unchanged, so the token axis stays a fair comparison. Fix from the description alone.
    return (
        "You are working in a checkout of the Django web framework. Resolve the following issue "
        "by EDITING THE SOURCE CODE in this repository (make the actual code changes with your "
        "editing tools -- do not just describe the fix). You have NO network access; work only "
        "from the code in this repository. Match the codebase's existing conventions.\n\n"
        "<issue>\n" + problem_statement.strip() + "\n</issue>\n\n"
        "When finished, briefly summarize the files and functions you changed and why."
    )


def run_agent(problem_statement: str, arm: str, worktree: Path, mcp_config: Path | None,
              model: str, max_turns: int, timeout: int) -> dict:
    cmd = [
        "claude", "-p", build_prompt(problem_statement),
        "--output-format", "stream-json", "--verbose",
        "--model", model, "--max-turns", str(max_turns),
        "--permission-mode", "bypassPermissions",
        "--strict-mcp-config",
        "--disallowedTools", "WebSearch", "WebFetch",  # no public-solution lookup
        "--add-dir", str(worktree),
    ]
    if arm == "B":
        # Graph availability = the auryn MCP; adoption is driven by the repo's own CLAUDE.md
        # (written into the worktree before this call), read at session start -- not a per-turn
        # system-prompt note (which the agent read but ignored). No token-axis contamination.
        cmd += ["--mcp-config", str(mcp_config)]

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
    init = parsed["init"]
    mcp_connected = any(
        str(s.get("name")) == "auryn" and str(s.get("status")).lower() not in ("failed", "error")
        for s in init.get("mcp_servers", [])
    )
    net_flags = _network_bash(parsed["tool_calls"])
    return {
        "arm": arm, "returncode": rc, "wall_seconds": wall,
        "mcp_connected": mcp_connected,
        "auryn_tools_available": len(init.get("auryn_tools", [])),
        "answer": result.get("result", ""),
        "cost_usd": result.get("total_cost_usd"),
        "num_turns": result.get("num_turns"),
        "tokens": {
            "input": usage.get("input_tokens", 0), "output": usage.get("output_tokens", 0),
            "cache_creation": usage.get("cache_creation_input_tokens", 0),
            "cache_read": usage.get("cache_read_input_tokens", 0),
            "total": usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0) + usage.get("cache_read_input_tokens", 0),
        },
        "tool_mix": classify_tools(parsed["tool_calls"]),
        "network_bash_flags": net_flags,
        "timeline": parsed["timeline"],
        "stderr_tail": (stderr or "")[-500:],
    }


def _network_bash(tool_calls: list[dict]) -> list[str]:
    flags = []
    for tc in tool_calls:
        if (tc.get("name") or "") == "Bash":
            cmd = str((tc.get("input") or {}).get("command", ""))
            if _NET_PAT.search(cmd):
                flags.append(cmd[:200])
    return flags


# -- grading (objective, via the PR's tests) -----------------------------------

def capture_model_patch(worktree: Path) -> dict:
    diff = _run(["git", "-C", str(worktree), "diff"]).stdout
    files = sorted({l[6:] for l in diff.splitlines() if l.startswith("+++ b/")})
    return {"diff": diff, "files_changed": files, "n_files": len(files), "diff_lines": diff.count("\n")}


def grade_by_tests(worktree: Path, test_patch: Path, test_modules: list[str], test_python: str,
                   timeout: int) -> dict:
    # Apply the hidden test patch ON TOP of the agent's edits, then run the affected modules.
    ap = _run(["git", "-C", str(worktree), "apply", str(test_patch)])
    if ap.returncode != 0:
        # try a 3-way in case the agent touched a neighboring line
        ap = _run(["git", "-C", str(worktree), "apply", "--3way", str(test_patch)])
        if ap.returncode != 0:
            return {"resolved": False, "error": f"test patch did not apply: {ap.stderr[-300:]}"}
    # Force the agent's worktree onto sys.path[0] so `import django` resolves to the edited
    # code, not any Django installed in the test venv (the venv must be deps-only).
    env = {**os.environ, "PYTHONPATH": str(worktree)}
    proc = _run([test_python, "runtests.py", *test_modules, "--parallel", "1", "--verbosity", "1"],
                cwd=str(worktree / "tests"), timeout=timeout, env=env)
    out = (proc.stdout + "\n" + proc.stderr)
    tail = out[-1200:]
    passed = proc.returncode == 0 and re.search(r"\nOK\b", out) is not None
    m = re.search(r"Ran (\d+) test", out)
    fail_m = re.search(r"FAILED \(([^)]*)\)", out)
    return {"resolved": bool(passed), "returncode": proc.returncode,
            "tests_run": int(m.group(1)) if m else None,
            "failure_summary": fail_m.group(1) if fail_m else None,
            "modules": test_modules, "output_tail": tail}


# -- orchestration -------------------------------------------------------------

def _test_modules(raw: list[str]) -> list[str]:
    # keep real test modules, drop fixture modules (management commands, model defs, etc.)
    return [m for m in raw if m.split(".")[-1].startswith("test") or m.split(".")[-1] == "tests"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="swe config.json (instance -> base_commit, test_modules)")
    ap.add_argument("--patches-dir", required=True, help="dir with <instance>.test.patch")
    ap.add_argument("--full-json", required=True, help="swebench_django_full.json (problem_statement per instance)")
    ap.add_argument("--django-src", required=True)
    ap.add_argument("--test-python", required=True, help="python in the django test venv")
    ap.add_argument("--work-root", required=True, help="dir to create worktrees under")
    ap.add_argument("--arms", default="A,B")
    ap.add_argument("--instances", default=None, help="comma list; default = all in config")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--max-turns", type=int, default=40)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--test-timeout", type=int, default=300)
    ap.add_argument("--graph-name", default="swe_django")
    ap.add_argument("--out", default=str(_HERE / "results-swe"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    os.environ["AURYN_AUTO_INSTALL"] = "0"
    cfg = json.loads(Path(args.config).read_text())
    full = json.loads(Path(args.full_json).read_text())
    ids = [s.strip() for s in args.instances.split(",")] if args.instances else list(cfg)
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    django_src = Path(args.django_src).resolve()
    work_root = Path(args.work_root).resolve()
    work_root.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out) / stamp
    (out_dir / "runs").mkdir(parents=True, exist_ok=True)

    plan = [(iid, arm) for iid in ids for arm in arms]
    print(f"instances: {ids}\narms: {arms}\nmodel: {args.model} max_turns={args.max_turns}\nout: {out_dir}")
    if args.dry_run:
        iid = ids[0]
        print("\n[dry-run] first:", iid, "base", cfg[iid]["base_commit"][:12],
              "test_modules", _test_modules(cfg[iid]["test_modules"]))
        print("prompt head:\n", build_prompt(full[iid]["problem_statement"])[:600])
        return

    runs = []
    for i, (iid, arm) in enumerate(plan, 1):
        print(f"\n[{i}/{len(plan)}] {iid} arm={arm} ...", flush=True)
        wt = work_root / f"{iid}__{arm}"
        prepare_worktree(django_src, cfg[iid]["base_commit"], wt)
        index_info = None
        mcp_config = None
        # Clean slate: arm B gets a FRESH per-instance graph (indexed here, deleted below), and
        # every run is a stateless `claude -p` (no memory/session/context carried between runs).
        graph_name = f"{args.graph_name}_{iid.rsplit('-', 1)[-1]}"
        try:
            if arm == "B":
                index_info = index_worktree(wt, graph_name)  # deletes any existing graph first
                mcp_config = _mcp_config_for(graph_name, out_dir, iid)
                _write_graph_claude_md(wt)  # adoption lever: the repo documents its graph
                print(f"    indexed: {index_info} (graph {graph_name})")
            run = run_agent(full[iid]["problem_statement"], arm, wt, mcp_config,
                            args.model, args.max_turns, args.timeout)
            run["model_patch"] = capture_model_patch(wt)
            run["grade"] = grade_by_tests(wt, Path(args.patches_dir) / f"{iid}.test.patch",
                                          _test_modules(cfg[iid]["test_modules"]), args.test_python, args.test_timeout)
        finally:
            remove_worktree(django_src, wt)
            if arm == "B":
                _delete_graph(graph_name)  # no graph outlives its run
        run["instance_id"] = iid
        run["index"] = index_info
        run["graph_name"] = graph_name if arm == "B" else None
        runs.append(run)
        g = run["grade"]
        print(f"    resolved={g.get('resolved')} tokens={run['tokens']['total']:,} "
              f"cost=${run['cost_usd']} time={run['wall_seconds']}s turns={run['num_turns']} "
              f"tools={run['tool_mix']} files={run['model_patch']['n_files']} "
              f"net_flags={len(run['network_bash_flags'])}")
        (out_dir / "runs" / f"{iid}__{arm}.json").write_text(json.dumps(run, indent=2))

    _summary(runs, out_dir, args)
    _print_table(runs)


def _summary(runs, out_dir, args):
    summary = {
        "timestamp": out_dir.name, "benchmark": "swe-bench-verified-django-codegen",
        "model": args.model, "max_turns": args.max_turns,
        "runs": [{k: r[k] for k in ("instance_id", "arm", "tokens", "cost_usd", "wall_seconds",
                                    "num_turns", "tool_mix", "network_bash_flags")}
                 | {"resolved": r["grade"].get("resolved"), "files_changed": r["model_patch"]["files_changed"]}
                 for r in runs],
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print(f"\nWrote {len(runs)} runs + summary.json to {out_dir}")


def _print_table(runs):
    by_arm: dict[str, list] = {}
    for r in runs:
        by_arm.setdefault(r["arm"], []).append(r)
    print("\n=== A/B means (Django code-gen; accuracy = test-resolved rate) ===")
    print(f"{'arm':<4} {'resolved':>9} {'tokens':>10} {'cost$':>8} {'time_s':>7} {'turns':>6} {'mcp/file':>9}")
    for arm, rs in sorted(by_arm.items()):
        n = len(rs)
        res = sum(1 for r in rs if r["grade"].get("resolved")) / n
        tok = sum(r["tokens"]["total"] for r in rs) / n
        cost = sum((r["cost_usd"] or 0) for r in rs) / n
        tm = sum(r["wall_seconds"] for r in rs) / n
        turns = sum((r["num_turns"] or 0) for r in rs) / n
        mcp = sum(r["tool_mix"]["mcp"] for r in rs) / n
        fil = sum(r["tool_mix"]["file"] for r in rs) / n
        print(f"{arm:<4} {res:>8.0%} {tok:>10,.0f} {cost:>8.3f} {tm:>7.1f} {turns:>6.1f} {mcp:>4.1f}/{fil:<4.1f}")


if __name__ == "__main__":
    main()
