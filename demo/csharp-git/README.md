# Demo: git-tracked C# ingestion

Moves a single project's graph through **three real commits** of a small,
hand-authored C# API, to show what `auryn git-sync` actually does: not "ingest
a snapshot," but keep one graph correct as the underlying codebase evolves --
forward, and (see below) backward too.

## What's in it

`setup.py` builds a throwaway local git repository at `sample-repo/` (safe to
re-run -- it's deleted and rebuilt from scratch every time) with three commits
telling one small, realistic story. Each commit is also tagged, so they're
selectable straight from the viewer's branch/tag dropdown -- no pasting commit
shas needed:

| Tag | Commit | What changes | Why it matters |
|---|---|---|---|
| `v1-core-domain` | "Core domain model and service layer" | `Widget`/`BaseEntity`/`WidgetStatus`/`Money`/`WidgetChangedHandler` in `Models/Entities.cs`; `IWidgetRepository`/`WidgetRepository`/`IWidgetService`/`WidgetService` in `Services/WidgetService.cs` | No API layer yet -- `Widget` is a plain `Class`. Exercises `EXTENDS` (`Widget : BaseEntity`), `IMPLEMENTS` (both services implementing their interfaces), `Enum`, `Record`, `Delegate` in one shot. |
| `v2-http-api` | "Expose Widgets over HTTP" | Adds `Controllers/WidgetsController.cs`, whose endpoints return `Widget` directly | **`Widget` flips from `Class` to `DTO`** -- and `Entities.cs`, where `Widget` is declared, isn't touched by this commit *at all*. |
| `v3-dedicated-dtos` | "Introduce dedicated request/response DTOs" | Adds `WidgetDto`/`CreateWidgetRequest` in `Models/Dtos.cs`; the controller (already-existing file, modified) switches to mapping through them instead of leaking `Widget` | **`Widget` flips back to a plain `Class`** -- again without `Entities.cs` ever being touched -- while the two brand-new classes take over the `DTO` label. |

Commit 2's flip is the scenario that ruled out deriving graph edits from a
file-level `git diff` in the first place (see the design discussion this feature
came out of): the file that needs its label changed isn't the file the commit
touched. Commit 3 shows the same mechanism working in reverse, entirely through
forward history -- no explicit "undo" logic anywhere, just the same `sync()`
recomputing the full picture each time.

## What syncing through it produces

```bash
uv run python demo/csharp-git/run_demo.py
```

drives all three commits in order and narrates each one. Measured on a truly
fresh graph:

```
Syncing to commit 1/3 (Core domain model and service layer)...
  nodes +51/-0/~0, edges +76/-0/~0
  Widget is currently labeled: Class

Syncing to commit 2/3 (Expose Widgets over HTTP)...
  nodes +15/-1/~2, edges +38/-15/~0
  Widget is currently labeled: DTO

Syncing to commit 3/3 (Introduce dedicated request/response DTOs)...
  nodes +13/-5/~5, edges +36/-24/~0
  Widget is currently labeled: Class
```

A few things worth poking at directly in the [viewer](../../README.md#viewing-ingested-graphs)
or via Cypher once it's open:

- **The `Widget` node keeps the same id across all three commits** -- it's the
  same graph node being relabeled and re-propertied in place, not a
  delete-and-recreate dance you'd have to reconcile yourself:
  ```cypher
  MATCH (n {name: 'Widget'}) RETURN labels(n)[0]
  ```
- **At commit 3, the endpoints' `ACCEPTS`/`RETURNS` edges point at the new DTOs,
  not `Widget`**:
  ```cypher
  MATCH (m:Method)-[:EXPOSES_ENDPOINT]->(e:APIEndpoint)
  OPTIONAL MATCH (e)-[:ACCEPTS]->(accept) OPTIONAL MATCH (e)-[:RETURNS]->(ret)
  RETURN m.name, accept.name, ret.name
  ```
- **The `Project` node records where it came from**: `repo_uri`, `commit_sha`,
  `ref` -- this is what feeds the "Git-tracked projects" panel in the viewer's
  sidebar, where `Widgets` shows up with a branch/tag dropdown already populated
  with `v1-core-domain` / `v2-http-api` / `v3-dedicated-dtos` (plus `main`) ready
  to pick from directly, no need to know or paste a commit sha.
- **Re-run the command again** (or pick the same tag again in the viewer and hit
  "Sync") and every stat is zero -- syncing to a commit the graph is already at is
  a genuine no-op, not a rewrite that happens to produce the same result.

## Run it

```bash
uv run python demo/csharp-git/run_demo.py
```

One command: starts FalkorDB via Docker if nothing's already listening on its
default port (reuses it on later runs), rebuilds `sample-repo/` from scratch,
syncs through all three commits while printing what changed at each step, then
opens the viewer. From there, keep moving between commits yourself from the
"Git-tracked projects" panel's dropdown for `Widgets` -- the run also prints the
exact `auryn git-sync` command for each tag if you'd rather drive it from the
CLI.

### Manual steps

```bash
uv run python demo/csharp-git/setup.py   # (re)builds sample-repo/, prints its tags

uv run auryn git-sync csharp \
    --repo-uri demo/csharp-git/sample-repo \
    --project-name Widgets --solution-name Acme \
    --ref v1-core-domain

uv run auryn view
```

Omitting `--ref` syncs to `sample-repo`'s current default branch tip (the third
commit). Re-running with a different `--ref` -- including an *earlier* one --
moves the same graph there directly; there's no need to "undo" anything first.
