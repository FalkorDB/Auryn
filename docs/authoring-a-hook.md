# Authoring a hook (post-ingestion enrichment)

A **hook** is a pass that runs *after* the language ingestors, reads the finished graph, and
synthesizes derived nodes/edges — the seams static extraction can't see on its own (e.g. the
C#↔PL/SQL message-routing layer). Hooks run automatically on **every** `ingest` **and** every
incremental `sync`, so a derived layer never goes stale.

There are two ways to author one. Pick by intent:

| | **Local drop-in** | **Plugin** |
|---|---|---|
| For | your own / ad-hoc hooks | shipping a hook in a distribution |
| Where | `~/.auryn/hooks/*.py` | an `auryn.enrichments` entry point |
| Setup | none — drop the file | package + `pip install`/`uv sync` |
| Source tag | `local` | `plugin` |

Both land in the **same** registry, run identically, and are toggled the same way.

---

## The easy path: a local drop-in

Scaffold one:

```bash
auryn hooks new link-tests      # writes ~/.auryn/hooks/link_tests.py
```

Then edit its `run`. The whole contract is one function — a top-level `run(writer, repo,
target_sha)` makes the file a hook (no imports of registry internals needed):

```python
# ~/.auryn/hooks/link_tests.py
from auryn.core.graph_model import GraphEdge

NAME = "link-tests"     # optional; the hook name defaults to the file's stem
# ORDER = 100           # optional; hooks run in ascending order (default 100)

def run(writer, repo, target_sha):
    # 1) idempotency — clear THIS hook's own layer first, so re-running is a no-op:
    writer.query("MATCH ()-[r:TESTS]->() DELETE r")
    # 2) read what the language ingestors already produced:
    rows = writer.ro_query(
        "MATCH (t:Function), (f:Function) "
        "WHERE t.name STARTS WITH 'test_' AND f.name = substring(t.name, 5) "
        "RETURN t.id, f.id"
    ).result_set
    # 3) synthesize + merge:
    writer.merge_edges([
        GraphEdge("TESTS", "Function", {"id": t}, "Function", {"id": f},
                  {"provenance": "link-tests"})
        for t, f in rows
    ])
    return {"tests_linked": len(rows)}   # a small report dict, or None if nothing to do
```

That's it — no `pyproject.toml`, no entry point, no reinstall. Confirm it's discovered:

```bash
auryn hooks list
#   link-tests     post  order=100   local   enabled
#   grpc-contract  post  order=100   plugin  enabled
```

It now runs at the tail of the next `ingest`/`sync`.

> Local hooks are **machine-wide** and user-owned (`~/.auryn/hooks/`), never read from a repo
> — so ingesting an untrusted clone can't execute a hook. A local file that names itself after an
> installed **plugin** hook is skipped (the plugin wins); a file that fails to import is reported by
> `hooks list` as `BROKEN`, and never aborts ingestion.

## The contract

`run(writer, repo, target_sha) -> dict | None`

- **`writer`** — a `GraphWriter` over the target graph. Read with `writer.ro_query(cypher,
  params).result_set`; write with `writer.merge_nodes(...)`, `writer.merge_edges(...)`, or
  `writer.query(...)` for deletes. New labels/edge types you own can be declared with
  `writer.setup_schema([...])`.
- **`repo`** — the already-fetched source. Read a specific input file with
  `repo.list_files(target_sha, "myfile.sql", set())` + `repo.read_blob(sha)` (this is how
  `grpc-contract` finds `.proto` files). Many hooks ignore `repo` and read only the graph.
- **`target_sha`** — the resolved commit / tree hash.
- **Return** a small report dict (shown in the ingest/sync summary), or `None` to signal "nothing
  to do" (e.g. your input file isn't in this repo).
- **Be idempotent.** Delete-then-rebuild your own layer every run. This is what makes re-running
  after an incremental `sync` correct against the post-delta graph, and keeps the graph regenerable.

## Shipping a hook as a plugin

When a hook should travel in a distribution (with its own deps, versioning, release), register it
via an entry point instead — see **Appendix B** of [adding-a-language.md](adding-a-language.md). The
reference implementations are `packages/grpc/`'s `grpc-contract` and
`packages/python/`'s `pyframework` (`python-routes`).

---

## Toggling hooks

Default is **opt-out**: a hook runs unless disabled.

```bash
auryn hooks disable link-tests            # install-wide, persists to config.toml
auryn hooks disable link-tests --repo .   # just this repo (additive to install-wide)
auryn hooks enable  link-tests            # re-enable

# one-off, no persistence:
auryn ingest dir . --skip-hook link-tests
auryn ingest dir . --only-hook grpc-contract   # run exactly this one (ignores disables)
```

`--only-hook`/`--skip-hook` (on `ingest`) and `--only`/`--skip` (on `enrich`) are per-run and
override the persisted config. Persisted disables live in `config.toml` under `[hooks] disabled`
(install-wide) and each `[[repo]]`'s `disabled_hooks` (per-repo).

## Notes / current limits

- Only **post** hooks execute today. `run` is not passed the incremental changeset — a hook does its
  normal full recompute on both the full and incremental paths (cheap: it's graph queries, not a
  file re-parse). Hooks are **not** language-gated — they run regardless of which languages were
  ingested; a hook that wants to skip irrelevant work should check its input and return `None`
  (as `grpc-contract` does).
- Hooks run on the repo-level paths (`ingest`, `sync`, and the commit-hook/watcher that shell out to
  `sync`). The low-level per-language `git-sync <lang>` command does **not** run hooks.
