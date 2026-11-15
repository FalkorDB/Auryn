# Auryn benchmarks

A baseline for "is this ingestor fast, and is it getting faster or slower over
time" -- something none of `tests/`, `demo/`, or the ingestors' own
`IngestionStats`/`SyncStats` answer on their own: they report a single wall-clock
number per run, but nothing persists it as a tracked baseline with pass/fail
assertions the way this suite does ([demo/csharp-large/](../demo/csharp-large/)
points at the same real codebase this suite does, but to let you look at the
result in the viewer, not to gate a regression).

**Not part of the normal test suite.** `uv run pytest` (no arguments) never picks
anything up here -- `pyproject.toml`'s `testpaths = ["tests"]` deliberately
excludes this directory, since these benchmarks clone a real, large public
repository over the network and can take several minutes. Always invoke this
directory explicitly.

## Framework

[`pytest-benchmark`](https://pytest-benchmark.readthedocs.io/) -- the standard
off-the-shelf choice, since it plugs directly into the project's existing
`pytest`/`uv` tooling and gives two things for free that matter for "baseline":

- `--benchmark-json=path.json` persists a run's numbers; `benchmarks/report.py`
  can re-print any saved file later without re-running anything:
  ```bash
  uv run python benchmarks/report.py benchmarks/results/latest.json
  ```
- [`pytest-benchmark compare`](https://pytest-benchmark.readthedocs.io/en/latest/comparing.html)
  diffs two saved runs against each other (e.g. before/after a future
  optimization).

Each case uses `benchmark.pedantic(fn, rounds=1, iterations=1)` rather than
pytest-benchmark's normal repeated-rounds default -- these are expensive,
real-network-and-real-FalkorDB operations (a full ingest can take minutes), not
microbenchmarks that make sense to run 20+ times per case.

pytest-benchmark's own terminal table (min/max/mean/stddev/ops/rounds) is built
for comparing many small microbenchmarks side by side and has nowhere to put
domain-specific numbers like node/edge counts or a parse-cache hit rate. So every
case also prints its own clean summary via the shared, zero-dependency
`benchmarks/report.py::print_summary` -- pytest-benchmark is the run/persistence/
comparison harness; it isn't what's responsible for readability.

## What's benchmarked: cold-ingesting a large public C# repo

`test_csharp_ingest_benchmark.py` targets
[`dotnet/aspnetcore`](https://github.com/dotnet/aspnetcore) by default -- verified
(not assumed) to be large and real before picking it: GitHub reports its size as
~377MB, and it's a real, actively-maintained, idiomatic ASP.NET Core codebase
(controllers, DI, DTOs, records -- a good match for what the C# ingestor
classifies). Three cases, sharing one graph and one warm parse cache, run in this
order:

1. **`test_cold_ingest`** -- fresh clone, fresh parse cache, full ingest at
   `cold_ref` into an empty graph. The worst-case "first contact" number.
2. **`test_noop_resync`** -- re-syncs to the *same* ref. Proves the fast-path: an
   unchanged commit costs one property lookup, not a re-parse (near-zero
   `files_parsed`, near-zero wall time).
3. **`test_incremental_resync`** -- moves the same graph to `warm_ref` (a newer
   real tag) with the warm parse cache from step 1 already in place. This is the
   number that actually demonstrates the point of `git_sync`'s blob-sha parse
   cache: `files_cached` should dwarf `files_parsed` even though the whole repo is
   large, because only the files that actually changed between the two releases
   ever get re-parsed.

`cold_ref`/`warm_ref` default to two adjacent real patch releases
(`v9.0.17` -> `v9.0.18`) rather than the branch tip, so the incremental case's
"only a handful of files changed" story is real and reproducible, not whatever
happens to be at `main` on the day you run it.

Want to see this same repo/ref pair in the [viewer](../README.md#viewing-ingested-graphs)
instead of a pytest assertion? See [demo/csharp-large/](../demo/csharp-large/) --
same operation, framed as something to look at rather than something to gate on.
It also shares this suite's git clone and parse cache under `~/.cache/auryn`,
so running one warms the other.

## Run it

```bash
uv run pytest benchmarks/ --benchmark-only -s \
    --benchmark-json=benchmarks/results/latest.json
```

- `--benchmark-only` skips anything that isn't a `benchmark`-fixture test (not
  load-bearing here since everything in this directory is one, but harmless and
  explicit).
- `-s` shows each case's live `print_summary` output as it runs (pytest captures
  stdout by default; without this you'd only see it on failure).
- `--benchmark-json=...` persists the run for later comparison/re-printing.

> **Benchmarks always run serially**, even though the main test suite defaults to
> parallel (`addopts = -n auto` in `pyproject.toml`). `benchmarks/conftest.py` pins
> the benchmark session to a single process in `pytest_configure`, so no `-n0` is
> needed on the command line. This is mandatory, not just a nicety: pytest-benchmark
> disables all timing under xdist, and the cases are order-dependent — `test_cold_ingest`
> populates the module-scoped graph + warm parse cache that `test_noop_resync`/
> `test_incremental_resync` reuse, so splitting them across workers would fail.

Expect this to take a few minutes the first time (cloning ~377MB and parsing tens
of thousands of `.cs` files) and to only need the network for the initial clone --
subsequent runs against the same `BENCHMARK_REPO_URI` fetch incrementally.

### Pointing it at a different repo

Every input is an environment variable, read once at the start of a run (see
`config.py`):

| Variable | Default | Meaning |
|---|---|---|
| `BENCHMARK_REPO_URI` | `https://github.com/dotnet/aspnetcore.git` | Repo to clone (anything `git` itself understands) |
| `BENCHMARK_PROJECT_NAME` | `AspNetCore` | Logical project name (also names the benchmark's FalkorDB graph: `benchmark_<lowercased>`) |
| `BENCHMARK_COLD_REF` | `v9.0.17` | The "before" ref for the cold-ingest and no-op cases |
| `BENCHMARK_WARM_REF` | `v9.0.18` | The "after" ref for the incremental-resync case |
| `BENCHMARK_WORKERS` | `os.cpu_count()` | Parse parallelism |
| `BENCHMARK_KEEP_GRAPH` | unset (cleans up) | Set to any value to leave the benchmark's FalkorDB graph in place afterward, e.g. to explore it in the [viewer](../README.md#viewing-ingested-graphs) |

For a much quicker sanity-check run against a small repo instead of the full
default, point `BENCHMARK_REPO_URI` at something small and set matching
`BENCHMARK_COLD_REF`/`BENCHMARK_WARM_REF` (any two real refs in that repo).

## Adding a benchmark for another ingestor

The pieces here aren't C#-specific: `conftest.py`'s `falkordb_connection` fixture
and `report.py`'s `print_summary` are plain, reusable building blocks. A SQL or
PL/SQL benchmark would follow the same shape -- its own `BenchmarkConfig`-style
env-driven settings, `benchmark.pedantic(..., rounds=1, iterations=1)` around the
real ingestor call, and a `print_summary` call with whatever `IngestionStats`
fields matter -- in its own `test_<name>_ingest_benchmark.py` file alongside this
one.
