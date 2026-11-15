# Demo: a real, large C# codebase (git-tracked ingestion at scale)

Every other C# demo ([`demo/csharp/`](../csharp/README.md),
[`demo/csharp-git/`](../csharp-git/README.md)) is a small, hand-authored sample
sized to exercise every entity/classification rule in one readable pass. This one
instead points `auryn git-sync` at a real, large, actively-maintained public
codebase -- [`dotnet/aspnetcore`](https://github.com/dotnet/aspnetcore) -- to show
the same ingestor holding up at actual scale: several thousand real `.cs` files,
hundreds of thousands of nodes and edges, not a few dozen.

It's the same repository, and the same two adjacent release tags, that
[`benchmarks/test_csharp_ingest_benchmark.py`](../../benchmarks/test_csharp_ingest_benchmark.py)
uses as its baseline -- see [`benchmarks/README.md`](../../benchmarks/README.md#whats-benchmarked-cold-ingesting-a-large-public-c-repo)
for why this repo was picked (verified large and real, not assumed, before being
chosen). The benchmark suite exists to catch a performance regression with an
assertion; this demo exists to let you actually look at the result -- same
operation, framed for the [viewer](../../README.md#viewing-ingested-graphs)
instead of `pytest`.

## What it does

Two `git_sync` calls against one dedicated graph (`csharp_large_demo`, kept
separate from the small demos' `auryn` so the two don't mix in the viewer's
graph picker):

1. **Cold ingest at `v9.0.17`** into an empty graph -- every `.cs` file in the repo
   gets parsed for the first time. The worst-case "first contact" cost.
2. **Incremental resync to `v9.0.18`**, a newer adjacent real tag, with the parse
   cache from step 1 already warm -- only the handful of files that actually
   changed between the two patch releases get re-parsed; the rest are parse-cache
   hits (keyed by git blob sha, not path), even though the repo as a whole is huge.

## What it produces

Measured on a real run:

```
Cold ingest (v9.0.17)...
  nodes +187997/-0/~0, edges +422831/-0/~0 (144.9s)
  9803 files parsed, 0 files reused from the parse cache

Incremental resync (adjacent real release) (v9.0.18)...
  nodes +30/-8/~20, edges +88/-18/~0 (13.4s)
  9 files parsed, 9794 files reused from the parse cache
```

That last line is the actual point of this demo: moving the graph forward one
real release took **13 seconds and 9 re-parsed files**, not another couple of
minutes and another 9803 -- even on a repo this size, `git_sync` only ever pays
for what actually changed. A few things worth looking at directly in the
[viewer](../../README.md#viewing-ingested-graphs) or via Cypher once it's open
(pick the `csharp_large_demo` graph):

- **Scale, at a glance**: `MATCH (n) RETURN labels(n)[0] AS label, count(*) ORDER BY
  count(*) DESC` -- thousands of `Method`/`Parameter`/`Class` nodes from a real,
  idiomatic ASP.NET Core codebase (controllers, DI, DTOs, records), not the
  dozens each small demo produces.
- **`APIEndpoint`/`DTO` classification at real scale**:
  `MATCH (e:APIEndpoint) RETURN count(*)` and `MATCH (d:DTO) RETURN count(*)` --
  the same classification rules the [small C# demo](../csharp/README.md) walks
  through one-by-one, just applied across a codebase with real controllers and
  real request/response types instead of one hand-built example of each.
- **The `Project` node's git provenance**: `MATCH (p:Project {name: 'AspNetCore'})
  RETURN p.commit_sha, p.ref` -- after the second sync, this points at `v9.0.18`,
  confirming the same `Project` node moved forward rather than a second one being
  created alongside it.
- **Re-run the script again**: every stat comes back zero -- syncing to a ref the
  graph is already at is a genuine no-op, the same fast-path
  [`demo/csharp-git/`](../csharp-git/README.md) demonstrates on a much smaller
  repo.

## Run it

```bash
uv run python demo/csharp-large/run_demo.py
```

One command: starts FalkorDB via Docker if nothing's already listening on its
default port (reuses it on later runs), clones `dotnet/aspnetcore` if it isn't
already cached, syncs through both tags while printing what changed at each step,
then opens the viewer.

**Expect the first run to take several minutes** -- cloning ~377MB and parsing
several thousand `.cs` files isn't fast, and that's the point: this demo exists
specifically to show the ingestor working at a size the other demos don't
attempt. The git clone and parse cache this leaves behind under
`~/.cache/auryn` are shared with [`benchmarks/`](../../benchmarks/README.md)
-- whichever you run first warms the cache for the other, so a second run of
either is much quicker.

### Manual steps

```bash
uv run auryn git-sync --graph-name csharp_large_demo csharp \
    --repo-uri https://github.com/dotnet/aspnetcore.git \
    --project-name AspNetCore \
    --ref v9.0.17    # then re-run with --ref v9.0.18 to see the incremental case

uv run auryn view
```

Then open `http://127.0.0.1:8080`, pick the `csharp_large_demo` graph, and (once
it's synced past `v9.0.17`) use the viewer's "Git-tracked projects" panel to move
`AspNetCore` to `v9.0.18` -- or any other tag/branch/commit in the repo -- yourself.

### Pointing it at a different repo

`REPO_URI`/`PROJECT_NAME`/`COLD_REF`/`WARM_REF` are plain module-level constants
at the top of `run_demo.py` -- edit them directly to point this at any other real
repo you want to see ingested at scale (anything `git` itself understands: local
path, `https://`, `ssh://`, ...).
