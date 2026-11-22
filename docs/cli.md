# `auryn` CLI reference

Every command is also self-documenting: `auryn <command> --help` (or `python -m auryn
<command> --help`). This page is the consolidated map of the whole surface.

The commands fall into six groups:

| group | commands |
|-------|----------|
| **Set up / tear down** | `install`, `uninstall`, `serve`, `stop` |
| **Ingest** | `ingest dir`, `ingest repo`, `quickstart`, `run <ingestor>`, `git-sync <ingestor>`, `enrich` |
| **Hooks** | `hooks list`, `hooks new`, `hooks enable`, `hooks disable` |
| **Keep fresh** | `sync`, `watch` |
| **Manage what's ingested** | `list`, `forget`, `stats` |
| **Use the graph** | `mcp`, `view` |

**Shared FalkorDB connection options** (on the connecting commands): `--host`, `--port`,
`--username`, `--password`, `--ssl`, and (where a single graph is targeted) `--graph-name`.
Omit them and the command uses the backend chosen at `install` time (a local bundled FalkorDB is
started on demand); the default graph is one per repository/directory.

---

## Set up / tear down

### `install`
Set up Auryn on this machine: choose a FalkorDB backend and register the MCP server with
every detected agent (e.g. Claude Code).
- `--backend local|remote` — bundled falkordb-lite vs. an existing server (prompted if omitted).
- `--host/--port/--username/--password/--ssl` — the remote server (with `--backend remote`).
- `--trigger manual|commit|save` — how the graph is kept fresh after an edit (prompted if omitted).
- `-y/--yes` — non-interactive (local backend unless `--backend` is given).

### `uninstall`
Reverse `install`: stop the local server and deregister Auryn from every agent.
- `--purge` — also delete `~/.auryn` (config **and all graphs**).
- `--yes` — skip the confirmation prompt.

### `serve`
Run the local bundled FalkorDB (falkordb-lite) in the foreground.
- `--port` — default: a random free loopback port. `--db-path` — default: `~/.auryn/store.db`.

### `stop`
Stop the local bundled FalkorDB daemon if running (no-op for a remote backend).

---

## Ingest

Two families:

- **`ingest` / `quickstart`** — the multi-language endpoint. Auto-detects and ingests **every
  supported language** under a source in one pass. This is how the tree-sitter languages
  (Python, C, C++, C#, Java, Go, Rust, PHP, Julia, TypeScript/JS) are ingested — they have no
  standalone command; they're detected by file suffix.
- **`run <ingestor>` / `git-sync <ingestor>`** — a single named ingestor, for sources that aren't
  just "a directory of files": live databases, an Oracle export, or when you want one language in
  isolation. Registered ingestors: `sql`, `oracle`, `oraschema`, `plsql`, `csharp` (`run`) and
  `csharp`, `plsql` (`git-sync`).

### `ingest dir <path>`
Ingest every supported file under a local directory, recursively (no git required). Re-running
derives a precise delta.
- `--project-name` (default: dir name), `--solution-name`.
- `--trigger manual|commit|save` — resync trigger registered for this repo.
- `--include-ext/--exclude-ext`, `--include-dir/--exclude-dir` — repeatable white/black-lists.
- `--skip-hook/--only-hook` (repeatable) — per-run post-hook overrides (see [hooks](#hooks)):
  `--skip-hook` drops a hook for this run, `--only-hook` runs exactly those (ignoring disables).
- `--deep inline|background|off` — deep semantic pass for two-phase languages (Python via Jedi,
  C# via Roslyn): `inline` (fast then deep, default), `background` (query the fast graph
  immediately), `off` (fast structural only — much quicker; no .NET SDK needed for C#).

Python's fast and deep passes use the same filtered file list, honoring the include/exclude
options and, for local git working trees, `.gitignore`. Generated files and vendored directories
(including CMake's `_deps/` downloads) are skipped by default; set `AURYN_INCLUDE_GENERATED=1`
to include them. To omit all dependencies explicitly, use `--exclude-dir deps`.
Python 2 source is not supported by the Python 3 parser: such files are logged and skipped,
not executed. Invalid escape sequences in otherwise parseable Python files produce
`SyntaxWarning` diagnostics with the source filename and line number.

### `ingest repo <repo_uri>`
Like `ingest dir` but for a git repository — a remote URI is `git clone`d into the current
directory first. Same `--project-name/--solution-name/--trigger/--include-*/--exclude-*/--deep`
options, plus `--ref` (branch/tag/commit; default: the repo's default branch).

### `quickstart [<path>]`
Zero-setup: ingest `PATH` into an **embedded** FalkorDB and open the viewer — no server, no Docker.
- `--db-path` (default: `<path>/.auryn/graph.db`), `--graph-name`, `--project-name`.
- `--host/--port` (viewer bind), `--no-open`, and the same `--include-*/--exclude-*/--deep` filters.

### `run <ingestor>` (shared: `--host/--port/--username/--password/--ssl/--graph-name`)
- **`run sql`** — reflect a live SQL database's schema (tables, columns, FKs, indexes,
  constraints, views, sequences). `--dsn` (SQLAlchemy URL), `--database-name`, `--schema` (repeatable).
- **`run oracle`** — Oracle PL/SQL from a **live** DB (packages, procedures, functions, triggers,
  types). `--dsn` (python-oracledb), `--database-name` (**must match `run sql`'s** to link),
  `--schema` (repeatable). Run `sql` first.
- **`run oraschema`** — reconstruct an Oracle schema catalog from a Redgate-style export folder.
  `--path` (export root), `--database-name`, `--strict` (+`--diagnostics-dir`), `--workers`,
  `--max-parse-seconds`.
- **`run plsql`** — a directory of PL/SQL **source files** (also extracts SQL DDL). `--path` or
  `--files-from FILE|-`, `--project-name`, `--strict` (default on; `--no-strict` for best-effort)
  `--diagnostics-dir`, `--workers`, `--max-parse-seconds`.
- **`run csharp`** — a C# codebase (classes, interfaces, methods, API endpoints, DTOs). `--path`,
  `--project-name`, `--solution-name`, `--target-framework`, `--dto-suffix` (repeatable), `--workers`.

### `git-sync <ingestor>` (shared connection options as `run`)
Ingest (first run) or move (every run after) a **git-tracked** project's graph to `--ref`.
- **`git-sync csharp`** — `--repo-uri`, `--project-name`, `--solution-name`, `--target-framework`,
  `--ref`, `--dto-suffix`, `--workers`, `--engine roslyn|treesitter` (roslyn = semantic + DI, needs
  the .NET SDK; treesitter otherwise).
- **`git-sync plsql`** — `--repo-uri`, `--project-name`, `--solution-name`, `--ref`, `--workers`.

### `enrich <path>`
Run post-ingestion **enrichment passes** over an already-ingested graph — they read a specific
input file under `PATH` (unfiltered) and synthesize derived edges onto what the language/DB
ingestors already built (e.g. the C#↔PL/SQL message-routing seam). Run it **after**
`run sql`/`run oracle`/`ingest`. `--only <name>` (repeatable) runs specific enrichments;
`--skip <name>` (repeatable, applied after `--only`) drops some; each is idempotent. Plus the
shared connection + `--graph-name` options.

> Enrichments run **automatically** at the tail of every `ingest` and every incremental `sync`
> (see [Hooks](#hooks)); `enrich` is the manual, unfiltered-input rerun for an already-ingested graph.

---

## Hooks

Post-ingestion **hooks** (a.k.a. graph enrichments) are passes that run after the language
ingestors finish — on **every** `ingest` **and** every incremental `sync` — reading the finished
graph and synthesizing derived nodes/edges (e.g. the C#↔PL/SQL routing seam). Two sources, one
registry: **plugins** shipped as `auryn.enrichments` entry points, and **local** drop-in files
under `~/.auryn/hooks/*.py`. A local file just defines `run(writer, repo, target_sha)` — no
packaging, no install. See [authoring-a-hook.md](authoring-a-hook.md).

### `hooks list`
Show every registered hook: name, phase, run order, source (`plugin`|`local`), and whether it's
enabled. `--repo <path>` shows enabled/disabled as it applies to that repo. Broken local files are
reported too.

### `hooks new <name>`
Scaffold a local hook at `~/.auryn/hooks/<name>.py` from a starter template (idempotent
`run` stub). `--force` overwrites.

### `hooks disable <name>` / `hooks enable <name>`
Turn a hook off/on. Persists to `config.toml` — **install-wide** by default, or **per-repo** with
`--repo <path>` (additive to the install-wide set). Default is opt-out: a hook runs unless disabled.
For a one-off run, use `ingest`'s `--skip-hook`/`--only-hook` instead of persisting.

---

## Keep fresh

### `sync [<path>]`
Resync a repo's graph **incrementally** — the target of the commit hook / save watcher (ADR 0002).
Reads the git diff + graph blast radius; re-parses only what changed. Post [hooks](#hooks) run at
the tail (honouring install-wide/per-repo disables). `--deep off|inline|background`
(default `off` = the fast freshness path) + connection options.

### `watch`
The save-trigger watcher daemon: resyncs a repo's graph when its files change (ADR 0003).
- `--foreground` (run in this process, blocking), `--stop`, `--status`. Normally managed
  automatically when the `save` trigger is chosen at install.

---

## Manage what's ingested

### `list`
List the repos ingested into the backend, with languages, commit shas, and node/edge counts.
Connection options only.

### `forget [<path>]`
Delete an ingested repo from the graph — the counter to `ingest`. Give a `<path>`, or `--graph`
(by graph name) / `--project` (one project in a shared multi-repo graph). `--dry-run` shows what
would be deleted; `--yes` skips the prompt. (Cross-repo references into the removed repo demote to
placeholders rather than vanish — ADR 0004.)

### `stats`
How an agent is using the code graph: MCP tool-call counts by operation, error rate, and result
sizes — visibility into how well the graph is being used. Reads the local usage log only.
- `--since 30m|24h|7d|<ISO>`, `--graph <name>`, `--by tool|op|session|graph`, `--json`.

---

## Use the graph

### `mcp`
Serve an MCP server exposing curated, read-only-by-default tools over one graph (stdio transport) —
the agent access layer. `--graph-name` (default: one per repo, from `CLAUDE_PROJECT_DIR`/cwd) +
connection options. Normally launched by the agent, not by hand.

### `view`
Serve a web UI for browsing every graph on a FalkorDB instance. `--host/--port` (viewer bind) and
`--falkordb-host/--falkordb-port/--falkordb-username/--falkordb-password/--falkordb-ssl`.
Bound to loopback (the default), the viewer answers only loopback Host names (`localhost`,
`127.0.0.1`) and the `--host` name itself — reached through an SSH alias or an `/etc/hosts`
name it returns 403 "Host not allowed"; start it with that name as `--host`.

### `embed`
Build vector + full-text indexes for a graph's callables/types/files — what powers the MCP
`search_code` (hybrid: lexical + semantic) tool. Idempotent (only un-embedded nodes are
processed). `--model` overrides the local embedding model; `--reembed` rebuilds everything (after
a model change); `--graph-name` + connection options. The local model auto-installs on first use
(opt-out; `AURYN_AUTO_INSTALL=0`).

### `impact`
"What will this change break?" — the blast radius of a diff. Diffs the working tree (or a git
range via `--base/--head`, or `--staged`), maps changed lines to graph symbols, and reports the
reverse-dependency closure: direct + transitive callers, API endpoints reached, tests covering it,
and cross-service consumers. `--json` for a PR bot; `--fail-on none|any|cross-service|endpoint`
exits non-zero to gate a merge in CI. `--graph-name` + connection options.

---

## Examples

```bash
auryn install -y                       # local backend, register the MCP server
auryn ingest dir .                     # ingest the current repo (all languages)
auryn ingest dir . --graph-name system # …into a SHARED graph (cross-repo edges; see demo/multi-repo)
auryn list                             # what's ingested
auryn sync .                           # incremental resync after edits
auryn stats --since 24h                # how the agent used the graph today
auryn view                             # browse it in the web UI
auryn run sql --dsn postgresql+psycopg2://u:p@host/db --database-name shop
auryn forget . --dry-run               # preview removal
```
