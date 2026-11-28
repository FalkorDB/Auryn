# ADR 0001 — Prospect onboarding & local server lifecycle

Status: **Accepted** (Phase 1 foundation implemented; `install`/`uninstall` and command
wiring pending; Phase 2 — the ingest/use flow — deferred).
Date: 2026-09-08.

## Context

A prospect on a clean machine should get from zero to "my already-installed coding agent
(e.g. Claude Code) can answer structural questions about my repo from a code graph" with as
little ceremony as Graphify's `uv tool install … / graphify install / /graphify .`. The
constraints we committed to:

- **Two distinct lifecycles.** *Installing/registering* Auryn (once per machine) is
  separate from *ingesting* a repo (once per repo, refreshed over time).
- **Backend is the user's choice at install time:** either a **remote/existing FalkorDB**
  (host/port/creds) or a **local, bundled FalkorDB** run via `falkordb-lite`
  (`falkordblite`) — no Docker.
- **Graph-per-repo by default**, but the CLI can ingest several codebases into one graph via
  `--graph-name`.
- **Never write anything into the user's repo.** All state lives centrally under
  `~/.auryn/`; agent registration is user-scoped (no `.mcp.json` committed into trees).
- **Viewer is on-demand** via a dedicated CLI command, never auto-started.
- **Solo evaluator now**, team-shareable later.
- **Agent self-ingests** when the graph is missing (the skill + `auryn_status` →
  `index_repository`).

## Decision

### 1. Two planes

| | Control plane — `install` | Data plane — `ingest` |
|---|---|---|
| Frequency | Once per machine | Once per repo (+ refresh) |
| Effect | Auryn present + **auto-registered with detected agents** (tools + skill) | A graph exists for a codebase |
| Trigger | Human, once | Usually the **agent**, on first structural question |

### 2. One runtime contract: everything is a client of a FalkorDB at `host:port`

Both backend choices collapse to "connect to a FalkorDB over host:port". The only difference
is *who runs it*:

- **remote** — the user points at an existing server; Auryn starts nothing.
- **local** — Auryn manages a **bundled `falkordb-lite` server** as a lazily-started,
  detached daemon (verified: falkordb-lite is a bundled `redis-server` child process, **not**
  Docker; an ordinary network client connects to it over TCP).

The single-process nature of an *embedded* falkordb-lite (it dies with its creating process —
verified) is why local mode is a **shared daemon** reachable over a port, not an in-process
DB: the agent's MCP server, the viewer, and the CLI are all separate processes that must see
the same graphs.

### 3. Two runtime processes, two owners — do not conflate

- **MCP server** (`auryn mcp`): the agent-access layer (tools). **The agent
  (Claude Code) spawns it** as a stdio child of each session, per the user-scoped
  registration `install` wrote. It is a *client* of the DB. Dies with the session. We never
  start it; the user never starts it.
- **FalkorDB daemon** (`auryn serve`): the database. Detached; survives sessions;
  cleared only by reboot or `auryn stop`.

Relationship: the agent starts the MCP server → the MCP server calls `ensure_server()` on
first tool use → that lazily cold-starts the local daemon (no-op for remote) → it connects.
So the MCP server is the *trigger* for the DB cold start but is itself agent-owned.

### 4. Lazy on-demand local daemon (random port + rendezvous)

Chosen over an OS service for the solo path: nothing is always-on; a cold start (~1s) after
reboot is acceptable. `ensure_server()`:

1. Read `run/server.json`; if its `pid` is alive and its `port` accepts a connection → use it.
2. Otherwise acquire `run/server.lock` (fcntl), re-check, and if still down spawn a
   **detached** (`setsid`, stdio → `run/serve.log`) `auryn serve`.
3. `serve` binds a **random free port** on **127.0.0.1 only**, writes `run/server.json`
   (`pid, port, db, started`), and blocks until SIGTERM.
4. The parent polls `server.json` until the port is live, then connects.

Random port avoids clashing with anything the user runs; the rendezvous file is how clients
discover it. Loopback-only bind keeps the local DB off the network.

### 5. `~/.auryn/` layout (one folder → clean uninstall)

```
~/.auryn/
  config.toml        # durable, user-owned: [backend] (local|remote + remote conn) + [install] manifest
  store.db           # the RDB — ALL graphs (one FalkorDB instance holds many named graphs)
  store.db.settings  # redislite sidecar
  run/               # ephemeral; safe to delete; recreated on next start
    server.json      #   { pid, port, db, started } — the rendezvous file
    server.lock      #   fcntl lock serializing cold starts
    serve.log        #   daemon stdout/stderr
```

Override the root with `AURYN_HOME` (tests, non-default installs). Reboot survival is
automatic for the files (config, rdb, registration in the agent's own config, skill); only
the *running* daemon is lazily respawned.

### 6. Graph identity from `CLAUDE_PROJECT_DIR`, not cwd

Verified via docs: Claude Code guarantees a `CLAUDE_PROJECT_DIR` **env var** on the spawned
MCP server (pointing at the project root) but **not** that the subprocess cwd equals it. So a
repo's default graph name derives from `CLAUDE_PROJECT_DIR` (fall back to cwd when absent, i.e.
direct CLI use). This makes **user-scoped registration + per-repo graphs** work with a single
install. `--graph-name` overrides to merge codebases.

### 7. Uninstall reverses an install manifest

`install` records what it touched (agents + config files, skill path, service?) into
`config.toml [install]`. `uninstall` reads it and reverses precisely — stop daemon,
deregister MCP per agent, remove skill, remove any service — never blind-editing configs it
didn't write. Data kept by default; `--purge` wipes `~/.auryn/`. Repos are never
touched. Removing the binary itself stays a separate `uv tool uninstall auryn`.

## Verified facts underpinning this ADR

- `falkordblite` bundles `redis-server` + `falkordb.so` and runs the server as a **child
  process** — no Docker/containers.
- An embedded falkordb-lite server **dies with its creating process**; the **rdb file
  persists**. → local mode must be a shared daemon, and reboot needs a lazy respawn.
- A bundled falkordb-lite server bound to a TCP port (`serverconfig={'port', 'bind'}`) is
  reachable by the **ordinary `falkordb` network client** from another process, bidirectionally.
- Claude Code: stdio MCP servers are **agent-spawned**; startup/`initialize` timeout defaults
  to **30s** (`MCP_TIMEOUT`/per-server `timeout` configurable); `CLAUDE_PROJECT_DIR` is set
  on the server env; stdio servers get **no auto-reconnect** on crash.

## Consequences

- The MCP server must answer `initialize` immediately and run `ensure_server()` on first
  tool call (not at startup) so a cold start / first ingest can't threaten the 30s handshake.
- The detached daemon must not inherit the MCP server's stdio (that stream *is* the MCP
  protocol) — it redirects to `run/serve.log`.
- Multiple agent sessions → multiple MCP servers → one shared daemon (the lock serializes the
  single cold start).
- The graph is derived/re-ingestable, so a write lost to a hard crash between RDB saves is
  recoverable by an incremental re-ingest; still, configure periodic save + save-on-shutdown.
- **Security:** loopback-only bind; `config.toml` should be `chmod 600`; prefer an env
  var/keyring over a plaintext remote password (to be enforced in `install`).
- **Windows:** the local backend is POSIX-only (no native `redis-server`; `fcntl`/signals/setsid
  assumed) and is **unsupported on native Windows**. This is now *enforced*, not just assumed:
  `ensure_server`/`run_server`/`quickstart` raise a clear "use remote, Docker, or WSL2" error
  instead of an `fcntl` ImportError, and `install`'s backend prompt drops the local option there
  (`core/platform_support.py`). WSL2 runs a Linux interpreter (`os.name == "posix"`), so the
  local backend works there unchanged; only native-Windows Python is excluded. Remote backend
  works everywhere.

## Status / not yet built

- **Done (Phase 1):**
  - `~/.auryn` paths + `config.toml` loader/writer (`core/home.py`, `core/app_config.py`).
  - Lazy daemon: `ensure_server()` + `serve`/`stop` (detached, random loopback port, rendezvous,
    fcntl lock, explicit redis-server pid kill) — `core/server_manager.py`.
  - `install` (backend prompt local|remote, user-scoped MCP registration + skill install with the
    detected agent, `[install]` manifest) and `uninstall` (manifest-driven reversal + `--purge`) —
    `core/install.py`, `core/agents.py` (Claude Code via `claude mcp add/remove -s user`), CLI
    `install`/`uninstall`. Verified against the real `claude` CLI (2.1.206).
  - `mcp`/`view`/`ingest` resolve their connection via `ensure_server(load_backend())` when no
    explicit `--host`; graph name defaults per-repo from `CLAUDE_PROJECT_DIR`/path (`core/project.py`).
    Verified end-to-end: `ingest dir` cold-starts the daemon and writes the derived graph.
- **Deferred:** Phase 2 (the ingest/use flow polish) and the opt-in OS-service lifecycle.
  (`run`/`git-sync` were wired to the backend config later: with no `--host` they resolve it
  like the commands above.)
