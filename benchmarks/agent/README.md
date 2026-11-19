# Auryn agent A/B benchmark

Does giving a coding agent (Claude Code) access to the code graph — via the `auryn`
MCP server — make it **cheaper (tokens), faster (time), and more accurate**? We measure by
running the *same unrestricted agent* on the *same graded questions* twice:

- **Arm A (no code graph)** — a normal Claude Code agent in the repo; no MCP
  (`--strict-mcp-config`), no priming.
- **Arm B (code graph)** — identical, plus the `auryn` MCP server + a concise priming.

Both arms may read/grep/glob freely — this tests *whether having the graph helps*, not
"graph vs files". Methodology adapted from Graphify's `BENCHMARKS.md`: fixed agent, capped
turns, key-fact-coverage grading with a citing judge, per-run spend ledger.

## Files

- `run_ab.py` — the harness: per (task × arm) `claude -p --output-format stream-json`,
  captures the full event timeline (thinking / tool calls / I/O), tokens/cost/time/turns,
  tool-mix, and MCP-connection status; grades answers by key-fact coverage
  `(covered + 0.5·partial)/total` via a citing judge.
- `tasks*.json` — graded tasks with gold key-facts verified against the target's source +
  code graph. `tasks-django.json` (round 1, easy) and `tasks-django-hard.json` (round 2,
  transitive) are the reported runs; `tasks.json` targets FalkorDB-py (pilot).
- `priming/` — the `AGENTS.md` + `auryn/SKILL.md` (source of truth; the harness injects
  a concise, tool-selecting version).
- `results/<timestamp>/` — per-run JSON logs + `summary.json`.
- `viewer.html` — interactive A/B viewer (amber = Arm A, teal = Arm B): per-task metrics +
  click-to-replay agent traces + graded facts; drag-drop more result sets to compare.

## Run

```bash
docker run -d -p 6379:6379 falkordb/falkordb:latest        # if FalkorDB isn't up
git clone https://github.com/django/django /tmp/django
uv run python benchmarks/agent/run_ab.py --repo /tmp/django \
    --tasks benchmarks/agent/tasks-django-hard.json --model sonnet
# then open viewer.html and drag in results/<stamp>/runs/*.json
```

Each agent run ≈ $0.1–1.1 depending on difficulty. Auth note: do NOT isolate
`CLAUDE_CONFIG_DIR` (auth is in the OS keychain → "Not logged in").

## Findings (Django, sonnet)

**The code graph's value is conditional — it scales with how expensive the file-reading
alternative is.**

| | Arm A (no graph) | Arm B (code graph) |
|---|---|---|
| **Round 1** (easy, greppable Qs) | 210K tok · $0.19 · 33s · cov 1.00 | 282K · $0.28 · 35s · cov 1.00 |
| **Round 2** (transitive closures / impact) | 466K · $0.72 · 208s · **cov 0.22** | 355K · $0.38 · **70s** · **cov 0.92** |

- **Round 1:** graph *lost* — on trivial questions grep is nearly free, so the MCP's fixed
  context overhead outweighed any saving, and accuracy tied at the ceiling.
- **Round 2:** graph *won decisively* — ~24% fewer tokens, ~47% cheaper, **3× faster, 4×
  more accurate**. Poster child (HT1, `reverse`'s full call closure): the no-graph agent
  burned **1.1M tokens / $1.08 / ~4 min reading 22 files and still failed (0.0)**; the graph
  agent answered correctly in **19s for $0.24** with one `behavioral_inventory` call. On HT2
  (a 463-function transitive blast radius) the no-graph arm **timed out entirely**.

What made round 2 work (fixes over round 1): **lean tool outputs** (drop `source`/bulky
props from list results), a **concise steering priming** that routes transitive/closure/
impact questions to the *single-call* tools (`behavioral_inventory`/`impact_of`/
`who_calls(max_depth)`), and **harder questions** where grep genuinely fails.

Takeaway: the graph pays off precisely on the hard, whole-repo, structural questions that
are the reason to have a code graph at all; on trivial lookups it's dead weight.
