# Code-generation A/B benchmark (SWE-bench Verified, Django)

Does giving a coding agent (Claude Code) access to the code graph — via the `auryn`
MCP server — make it better at **writing code**: cheaper (tokens), faster (time), and more
**accurate**? Unlike the Q&A benchmark (`README.md`, judge-graded key-fact coverage), this
one is about *editing a real codebase to resolve a real issue*, and accuracy is graded
**objectively by the issue's own tests** — no judge.

## Method (SWE-bench, run as A/B)

Tasks are real Django issues from **SWE-bench Verified** (human-curated). Per (instance × arm):

1. **Worktree** of `django/django` at the issue's **parent commit** (pre-fix state).
2. **Arm B only:** index that worktree into a fresh code graph (fast `ast` engine, ~10s for
   all ~2.7k files).
3. **Agent run** — headless `claude -p`, fully unrestricted tools — given **only the issue
   text**. It edits the source. It never sees the tests; there is no issue id, no hint a PR
   exists; **network is off** (`WebSearch`/`WebFetch` disallowed; any lookup-style Bash is
   flagged).
4. **Grade — objective:** apply the PR's **test patch** on top of the agent's edits and run
   the affected test modules (`runtests.py --parallel 1`). The `FAIL_TO_PASS` tests fail on
   the unfixed code, so **modules pass ⟺ the agent's fix is correct**. Binary, reproducible.

Arms (identical except the graph):
- **A (no code graph):** agent + repo. No MCP (`--strict-mcp-config`), no priming.
- **B (code graph):** same + the `auryn` MCP server + a code-gen priming that steers the
  agent to **localize and assess impact with the graph before editing** (`locate` →
  `who_calls`/`impact_of`/`behavioral_inventory`).

## Why this is the right design

- **Objective accuracy.** The gold *code* patch is never shown or on disk; we only use the
  PR's *tests* to score. No subjective judge, unlike the Q&A benchmark.
- **Cheating is controlled, not hoped-away.** Hiding the issue id is necessary but weak on
  its own; the real controls are **(a) no network in either arm** — eliminates live lookup
  of the public PR — and **(b) the tests are absent during the agent run** — the agent can't
  read the expected behavior. Traces are scanned for network Bash as a tripwire.
- **Training-data memorization** (Django is in every model's weights) is a **constant across
  both arms** — same model, same weights — so it inflates A and B equally and **cancels in
  the B − A delta**, which is what we report.
- **Where the graph should help: localization + impact.** The payoff is finding *where* to
  change and *what it affects* — exactly `who_calls`/`impact_of`/`behavioral_inventory`.
  Tasks are chosen across a difficulty/localization gradient so we can see where it pays off
  (and, like Round 1 of the Q&A benchmark, where it's dead weight on trivially-local fixes).

## Tasks (Django 5.0, one shared test env)

| instance | difficulty | gold source files | flavor |
|---|---|---|---|
| django-16667 | 15 min | `forms/widgets.py` (1) | easy, localized — *control* |
| django-16454 | 15 min–1 h | `core/management/base.py` (1) | mid |
| django-16938 | 15 min–1 h | serializers `python.py`+`xml_serializer.py` (2) | mid, multi-file |
| django-16631 | 1–4 h | `auth/__init__.py`+`auth/base_user.py` (2) | hard, multi-file, enhancement |

## Run

```bash
docker run -d -p 6379:6379 falkordb/falkordb:latest        # if FalkorDB isn't up
# a deps-only Django test venv (NO Django installed — runtests uses the worktree via PYTHONPATH):
python3.12 -m venv djvenv && ./djvenv/bin/pip install asgiref sqlparse tzdata PyYAML tblib
# instance metadata (base_commit, test_patch, FAIL_TO_PASS/PASS_TO_PASS) from SWE-bench Verified
uv run python benchmarks/agent/run_swe.py \
    --config <swe>/config.json --patches-dir <swe> --full-json <swe>/swebench_django_full.json \
    --django-src <django-clone> --test-python <swe>/djvenv/bin/python \
    --work-root <swe>/swe-work --arms A,B --model sonnet
```

`run_swe.py` reuses `run_ab.py`'s stream-json parsing / MCP-config / tool-mix machinery; only
the task (edit, not answer) and grading (tests, not judge) differ.

## Findings

_(filled in after the first run — resolution rate, tokens, time per arm, plus whether the
graph arm localized with fewer file reads before its first edit.)_
