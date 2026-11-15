# ADR 0003 — Repo lifecycle in the graph: forget/list + resync triggers

Status: **Accepted / in progress.** Implemented: `list`, `forget`, the config-backed ingest
registry, install-time trigger choice, the `sync` command, **`commit` git-hooks** (commit →
post-commit hook → `auryn sync` → incremental resync, verified end-to-end),
**clone-into-CWD** for URI ingest (`ingest repo <uri>` → `git clone` into CWD → ingest the
checkout → triggers attach), and the **`save` watcher daemon** (`core/watcher.py`: one detached
watchdog process per machine watches every save-triggered repo's working dir, debounces a burst
of saves, routes a changed path to its owning repo by longest-prefix, and fires a non-overlapping
`auryn sync` — verified end-to-end: edit → resync → graph updated). Remaining: cross-repo
resolution. Date: 2026-09-13.

Builds on ADR 0001 (install/ingest planes, `~/.auryn/`, graph-per-repo) and ADR 0002
(incremental resync).

## Context

Ingesting a repo has no inverse — you can add a codebase to the graph but not remove it (only
`uninstall --purge`, which nukes *everything*). And keeping the graph fresh is still
manual/agent-initiated; we want a save/commit to trigger a resync automatically. Both must
work when several repos share one graph (`--graph-name`). This ADR settles the *data-plane
lifecycle*: how a repo is identified, removed, and kept fresh.

## Decisions

### 1. Every ingested repo is backed by a local working directory

- **Local-path ingest** — the user's own checkout (what they edit).
- **URI ingest** — `auryn ingest <uri>` runs a plain `git clone <uri>` **into the current
  directory** (`./<name>/`, erroring if it exists — exactly as if the user ran it), then
  ingests that working copy. It's now an ordinary local checkout they edit/commit. (Replaces
  the bare-cache clone for URI ingest.)

So triggers and resync operate on a working directory in every case. A URI-cloned repo nobody
edits is refreshed by `git pull` (manual or scheduled), which advances HEAD → the resync
follows; save/commit triggers are for repos actually being edited.

### 2. Identity: derive it, don't configure it

- **`graph_name`** (which FalkorDB graph) is already path-unique: `default_graph_name` =
  `basename + 8-hex(hash(abspath))`, so two dirs named `acme` map to different graphs. This is
  what guarantees isolation in the default graph-per-repo world.
- **`project_name`** (the `<lang>://<name>` id namespace inside a graph) is just the
  **directory name**. It is *derived, not a stored/configured knob*. It needs to be unique only
  *within one graph key*; graph-per-repo makes that automatic. The **one** case the plain name
  is insufficient is a shared multi-repo graph holding two same-basename repos — there we
  auto-disambiguate (append the same path hash `graph_name` uses); an explicit `--project-name`
  remains as an escape hatch.

### 3. `forget` / `list` (the counter to ingest)

- **`auryn list`** — enumerate the backend's graphs that carry a `:Project` node (the
  code graph marker), showing per graph: project name(s), languages, commit sha, node/edge
  counts. Works entirely off the graph; no registry needed.
- **`auryn forget <path|--graph|--project>`** — two modes:
  - **Dedicated graph** (the repo is the only project in its graph → default case):
    `GRAPH.DELETE <graph_name>`. One call, complete.
  - **Shared graph**: project-scoped delete — for each language the repo contributed, delete
    `id == <lang>://<name>` and `<lang>://<name>/ ≤ id < <lang>://<name>/￿`. The **trailing
    `/` is load-bearing**: it stops `forget acme` from also deleting `acme-api` (the
    prefix-collision hazard is data loss here). Per-label, id-indexed (verified by the ADR-0002
    index discipline). `DETACH DELETE` removes each node *with its relationships*, so **inbound
    cross-repo edges** into the forgotten repo are cleaned up automatically.
  - Guarded by a confirmation showing what will go (graph/project, node count); `--yes` skips,
    `--dry-run` only prints. Also deregisters the repo's trigger and drops its clone/cache.

### 4. A minimal registry (only what git/the graph can't give us)

A `[[repo]]` table in `config.toml`, **keyed by `local_dir`** (the one field that's genuinely
not derivable — the URI clone lands in the user's CWD, their own checkout is wherever it is).
Everything else is derived on demand: the remote from `git -C <local_dir> remote get-url
origin`, `graph_name`/`project_name` from the path, languages/shas/counts from the graph. So a
common entry is essentially just `local_dir` (+ `trigger` only when it overrides the install
default, + `graph_name`/`project_name` only when a shared graph / custom name was used). Its
real job is the **watcher's watch-list** and the `local_dir ↔ graph` mapping. `list`/`forget`
don't need it (they work off the graph + derivation); the triggers do.

### 5. Resync triggers, chosen at install, carried per-ingest

`auryn install` picks `[resync].trigger` = `manual` | `commit` | `save` (default
**`commit`**; `manual` = today's on-demand behavior). Every `ingest` reads it and registers the
matching mechanism for that repo (per-ingest `--trigger` override allowed):

- **`commit` → git hooks (no daemon).** Install `post-commit`/`post-checkout`/`post-merge`/
  `post-rewrite` in `.git/hooks/`, chained inside guard markers (never overwrite; removed on
  `forget`/`uninstall`). Each runs `auryn sync <repo>` → incremental resync
  (`ingest_repository(ref=HEAD)`), detached so a commit isn't slowed.
- **`save` → one watcher daemon** (managed under `~/.auryn/run/`, like the FalkorDB
  daemon; `watchdog`, pulled in only when `save` is chosen). Watches every save-triggered repo's
  working dir (excluding `.git`/vendored/build dirs, filtered to ingested suffixes), debounces
  (~300ms), coalesces a burst into one resync, routes a changed path to its repo (longest
  registered-root prefix), and runs the working-tree incremental (`ingest_directory`).

**Multi-repo graph:** the trigger is per-repo, the resync is per-project, so a commit/save in
repo A resyncs only `<lang>://A` (scoped + last-good) — B/C in the same graph are untouched
(the multi-repo isolation test). Concurrent writes to a shared graph serialize per graph key
and touch disjoint prefixes; a per-repo lock in `run/` prevents a watcher and an agent resync
of the same repo overlapping. Broken-code last-good handling (ADR 0002) applies for free.

`save` and `commit` are clean alternatives, not layers: a commit changes no working-tree
content (`tree_sha` unchanged → resync short-circuits), and `commit` deliberately shows only
committed state — which is why one choice at install is the right model.

### 6. Multi-repo resolution model (graph as a global symbol table)

When a delta discovers a call to `X`: resolve local-first from the AST; if unbound, **consult
the graph for candidates across all projects** (ADR-0002 v2 name-targeted lookup, prefix
widened from `<lang>://A` to the whole graph), disambiguated by AST hints (receiver, import,
arity), conservative on ambiguity. So `A.foo → B.X` cross-repo edges are drawn by A's own
resolution (not enrichments), and A's scoped read maintains its *outbound* cross-repo edges.

- **Reverse direction is eventual-consistency (for now).** When B changes `X`, deleting `X`
  cleans the inbound edge (DETACH), but *creating/moving* A's edge won't happen until A next
  resyncs — the closure finds cross-repo callers but can't yet map them to files in another
  repo. Exact cross-repo closure is a follow-up.
- **Enrichment staleness.** Incremental resync doesn't re-run the enrichment pass, so
  enrichment-derived (incl. cross-language) edges refresh only on a full ingest. To decide:
  re-run affected enrichments after a trigger vs. periodic full ingest.

## Consequences / phasing

1. **`list` + `forget`** — self-contained, no daemon, off the graph. *Done.*
2. **Registry + install trigger + `sync` + `commit` git-hooks + clone-into-CWD** —
   commit-triggered freshness. *Done* (`core/app_config` registry, `core/hooks`, CLI
   `sync`/`install --trigger`/`ingest --trigger`, `ingest repo` clone-into-CWD, `forget`
   deregistration; `tests/core/test_registry_hooks.py`, `test_clone_into_cwd.py`).
3. **`save` watcher daemon** — save-triggered freshness. *Done* (`core/watcher.py`:
   detached-daemon lifecycle mirroring `server_manager`, per-repo watchdog observers reconciled
   against the registry, debounced + prefix-routed + non-overlapping `auryn sync`; CLI
   `watch --foreground/--stop/--status`, wired from `install`/`ingest --trigger save` via
   `ensure_watcher()` and torn down by `uninstall`; `tests/core/test_watcher.py`).
4. **Cross-repo resolution** (§6) — its own phase; the biggest correctness surface.
