# Freshness benchmark

**How expensive is it to keep the graph up to date after a small source edit?**

`tests/` and the other benchmarks answer "how fast is a cold ingest." This one
answers the question that actually dominates day-to-day cost: a developer saves a
file, and the graph has to catch up. The re-ingest model (see
[`core/graph_sync.py`](../../packages/core/src/auryn/core/graph_sync.py)) never
diffs at the source level — for the changed language it **recomputes the whole
desired target state** (parse every file, resolve globally), **reads the entire
current subgraph back** out of FalkorDB to diff against, and **writes only the
delta**. So the cost of reflecting a one-line change is almost entirely
*independent of how small the change is*. This harness measures exactly that.

## What it measures

Every resync is timed broken down by phase (via the off-by-default
[`core/instrument.py`](../../packages/core/src/auryn/core/instrument.py), which
this harness turns on around each resync):

```
total = parse + resolve + read_fingerprints + diff + write   (+ other)
        \_______________________/   \_________________________________/
          recompute target state       reconcile with the graph
```

- **parse / resolve** — recompute the target state (Python only reports these two
  separately today; other languages lump into `other`).
- **read_fingerprints** — the whole-subgraph read-back to diff against. Profiled at
  **~87% of an incremental resync** on dotnet/aspnetcore (see the
  `_SUBGRAPH_READ_WORKERS` note in
  [`graph_client.py`](../../packages/core/src/auryn/core/graph_client.py)). Its
  cost scales with **total graph size**, not with how much changed.
- **diff** — `compute_delta` (in-memory fingerprint comparison).
- **write** — `apply_delta` (the batched MERGE/DELETE of the actual delta).
- **other** — schema setup, language detection, enrichment, walk/overhead.

Plus, per scenario: how many nodes/edges were **actually written or deleted**
(`Δnodes`/`Δedges`) and how many files were parsed vs. served from cache.

## How it stays honest

- **One warm baseline, replayed.** A full cold ingest warms the graph once. Every
  scenario is measured against *that same* baseline: after the measured resync the
  mutation is undone and an **unmeasured "restore" resync** returns the graph to
  the baseline, so scenarios never accumulate.
- **A planted sentinel module.** A synthetic module (`zzz_freshness_bench_*.py`,
  planted before the cold ingest) gives the entity-level scenarios — edit a body,
  change a signature, add/delete a function/file — crisp, **repo-independent**
  deltas, while the surrounding **real** tree provides the read-back scale that
  makes the numbers meaningful. The number of functions in the sentinel
  (`--n-functions`, default 40) is what makes the `comment_top` line-renumber
  churn visible.
- **The real ingest path.** It calls `ingest_directory` — the same entry point the
  MCP `index_repository` tool uses — over a `LocalDirectorySource`, so a file edit
  is picked up by content hash exactly as in production.

## Scenarios

| Scenario | Edit | What it isolates |
|---|---|---|
| `noop` | nothing | The commit/tree-sha short-circuit (should be ~free). |
| `comment_eof` | comment at end-of-file | A change that shifts **no** entity — pure read-back/parse waste. |
| `comment_top` | comment at top-of-file | Renumbers every entity below it → line-number churn in the fingerprint. |
| `whitespace` | trailing blank lines | Like `comment_eof`. |
| `edit_body` | change one function body | Smallest real single-entity delta. |
| `edit_signature` | add a parameter | Single entity + its parameters (+ any caller-edge churn). |
| `add_function` | add one function | Pure insert. |
| `delete_function` | remove one function | Pure delete (stale-node removal). |
| `add_file` | new source file | Insert of a whole file's subgraph. |
| `delete_file` | remove a source file | Delete of a whole file's subgraph. |
| `rename_file` | rename a source file | Path-keyed ids → delete-old + add-new. |
| `bulk_edit` | touch N real files | The case where full re-work is justified; establishes the crossover. |

## Run it

Self-contained: no pytest, no running server required (uses an **embedded**
FalkorDB by default, or a `localhost:6379` server if one is up).

```bash
# Against a local source tree (no network) — fastest way to a baseline:
uv run python benchmarks/freshness/run_freshness_benchmark.py \
    --repo-dir packages --project-name auryn-src \
    --out benchmarks/results/freshness.json

# Against a real public repo (clones it) — realistic scale for read_fingerprints:
uv run python benchmarks/freshness/run_freshness_benchmark.py \
    --repo-uri https://github.com/django/django.git --ref 5.1 --project-name django \
    --out benchmarks/results/freshness-django.json
```

Read a saved run back without re-running: the JSON has `config`, `cold`, and a
`scenarios` array of per-scenario metrics.

### Flags (all also `FRESHNESS_BENCH_*` env vars)

| Flag | Default | Meaning |
|---|---|---|
| `--repo-dir` | — | Local source tree to copy + benchmark (no network). |
| `--repo-uri` / `--ref` | — | Git repo to shallow-clone + benchmark. |
| `--project-name` | `freshness-bench` | Project/graph name. |
| `--deep` | `off` | `off` = fast/structural path; `inline` also runs deep engines (Python Jedi). |
| `--falkordb` | `auto` | `auto` (localhost then embedded), `embedded`, or `host:port`. |
| `--n-functions` | `40` | Functions in the planted sentinel module. |
| `--bulk-files` | `25` | Files the `bulk_edit` scenario touches. |
| `--scenarios` | all | Comma-separated subset to run. |
| `--out` | — | Write full results JSON here. |
| `--keep-workspace` | off | Keep the throwaway workspace for inspection. |

## Incremental resync (ADR 0002) — full vs. incremental

`measure_incremental.py` is the payoff harness for the incremental prototype: it cold-ingests
a tree, then for each small edit runs **both** a full `sync_at` and an `incremental_ast_sync`
(git-diff changed set + graph-derived affected closure; see
[ADR 0002](../../docs/adr/0002-incremental-resync.md)) from the same warm baseline and prints
their phase breakdowns and speedup side by side.

```bash
uv run python benchmarks/freshness/measure_incremental.py \
    --repo-uri https://github.com/django/django.git --ref 5.1 --project-name django
```

Correctness is not assumed — it's proven separately by the **equivalence oracle**
`tests/ingestors/test_python_incremental_equivalence.py`, which asserts the incremental
result is node-for-node, edge-for-edge identical to a full recompute across the scenario
matrix (rename, signature change, delete, add, cross-file reverse-deps, …). One scenario is
an `xfail` documenting a known prototype gap (a newly-satisfiable reference through an import,
which the `ast` engine leaves no breadcrumb for — needs IMPORTS edges).

## Reading the results

The two things to watch:

1. **`rdbk%` (read_fingerprints as a share of total)** grows with the graph, not
   with the edit. On a tiny tree parse dominates; at real scale the read-back is
   the tall pole — that's the number the "local fingerprint shadow" optimization
   targets (diff against a local copy instead of reading the whole graph back).
2. **`Δnodes` vs. the edit.** A `comment_top` that rewrites dozens of nodes for a
   semantically-null change is line-number churn in the fingerprint; a `noop` or
   `comment_eof` that still writes a node or two is the always-changing
   `Project.commit_sha`. Both are cheap wins independent of the read-back.
