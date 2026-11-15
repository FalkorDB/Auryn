# Agent-native context injection (PostToolUse) — hook + benchmark

Roadmap feature #2. The agent's reflex is `Grep`/`Read`/`Bash`, **not** our MCP tools — so instead
of asking it to change behaviour, a **PostToolUse hook** enriches the tools it already uses with
what they structurally can't see: a symbol's callers, callees, blast radius, and related code,
pulled from the code graph. Zero behaviour change required; the graph shows up in the flow.

## What it acts on

The hook (`auryn/hooks/postuse_enrich.py`) matches these tools:

| Tool | Enrichment | Gate |
|---|---|---|
| `Grep` | pattern → symbol def + callers + callees + related | fires only on a confident, real-definition symbol match (1–3 hits); regexes with many identifiers, phrases, and cross-repo/stdlib placeholders are suppressed |
| `Bash` (`rg`/`grep`/`cat`) | same as Grep/Read, for searches run through the shell | parses the command (quote-aware); non-search commands emit nothing |
| `Read` | file → symbols it defines + their **external** dependents (the file's blast radius) | only source files that map to graph symbols with external callers |
| `Edit`/`Write`/`MultiEdit` | proactive **impact-on-edit**: blast radius of the working-tree change (callers, tests, cross-service) | only when the diff maps to changed symbols with impact |

## How the caller tells hook output from tool output

Claude Code injects `hookSpecificOutput.additionalContext` as an **unattributed `<system-reminder>`
of plain text** — there is *no* built-in marker saying it came from a hook. So provenance is on us.
Every injection is wrapped in a self-labelling envelope:

```
⟦auryn⟧  (auto-injected by the code graph hook — NOT part of the tool result; structural facts, verify before acting)
  trigger: Grep "get_resolver"   ·   graph=django (106553 nodes)
get_resolver → Function  django.urls.resolvers.get_resolver:109
  called by (14): is_language_prefix_patterns_used · _get_view_func · … +6 more
  calls (1): _get_cached_resolver
⟦/auryn⟧
```

- distinctive `⟦auryn⟧…⟦/auryn⟧` sentinel (never collides with code/grep output);
- first line states the source + "NOT part of the tool result" + "verify before acting";
- `trigger:` ties the block to the exact tool call;
- only graph facts (who-calls-who, locations) — never quoted file text — so it's categorically
  different from what Read/Grep return;
- graph freshness stamp; per-fact `resolution` when present (folds in feature #5).

## Keeping it cheap (the whole point)

Fail-silent · gated · capped (`AURYN_HOOK_MAX_TOKENS`, default 220) · per-session dedup ·
tight `timeout`. Tunables are all env vars (`AURYN_HOOK_*`) so the benchmark can vary them.

## Enabling it (settings.json)

```json
{
  "hooks": {
    "PostToolUse": [
      { "matcher": "Grep|Read|Bash|Edit|Write|MultiEdit",
        "hooks": [{ "type": "command",
          "command": "AURYN_HOOK_GRAPH=<graph> <venv>/bin/python -m auryn.hooks.postuse_enrich",
          "timeout": 15 }] }
    ]
  }
}
```

## Measuring impact

### 1. Overhead micro-benchmark (cheap, no agents) — `replay.py`

Go/no-go gate: replays real reflex-tool inputs (harvested from A/B transcripts) + graph-sampled
events through `enrich()`, reporting fire rate, suppression breakdown, injected-token distribution,
and latency. Answers "how much does each tool call cost" before spending on agent runs.

```bash
uv run python benchmarks/hook/replay.py --graph django --repo /path/to/django \
  --from-transcripts benchmarks/agent/results-swe --sample-from-graph 150 --measure-coldstart
```

### 2. Agent A/B/C/D — `run_ab.py` + `abcd_report.py`

Same unrestricted agent, same graded questions, four arms:

- **A** — bare agent (no graph).
- **B** — MCP graph available + priming (must be *adopted*).
- **C** — **PostToolUse hook** enriching Grep/Read/Bash/Edit (no MCP, no priming): pure injection.
- **D** — hook + MCP + priming together.

```bash
uv run python benchmarks/agent/run_ab.py --repo /path/to/django \
  --tasks benchmarks/agent/tasks-django-hard.json --arms A,B,C,D --reps 3
uv run python benchmarks/agent/abcd_report.py \
  --bucket easy:benchmarks/agent/results-abcd-easy/<stamp> \
  --bucket hard:benchmarks/agent/results-abcd-hard/<stamp>
```

Metrics: tokens (incl. hook-injected tokens), cost, wall-time, turns, tool-mix, key-fact coverage,
plus hook fire/suppress counts and **influence** (did injected symbols reappear in later tool calls
/ the answer). Results are **bucketed by difficulty** because the sign of the token effect flips
between easy and hard questions.

### Pre-registered pass/fail gate

- **G1** no regression: coverage(C) ≥ coverage(A) in *every* bucket.
- **G2** real hard-bucket lift: coverage(C) − coverage(A) ≥ 0.15 on the hard bucket.
- **G3** easy-bucket overhead ceiling: tokens(C) ≤ tokens(A) × 1.10.

Plus the justification number: hard-bucket Δtokens ÷ Δcoverage (cost per unit of quality).
