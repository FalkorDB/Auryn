# Auryn — "Developers" page content

*Install, the get-started tutorial, the command & tool reference, language support, and agent
integration. Mirrors Graphify's Developers/Quickstart/Docs pages.*

---

## Install

```bash
# 1. Install the CLI (bundled local database included)
uv tool install falkordb-auryn        # or: pipx install falkordb-auryn

# 2. Register it with your coding agent + pick a backend
auryn install
```

`auryn install` asks one question — **how should it run FalkorDB?**

- **Local (recommended)** — a bundled, serverless database (falkordb-lite) that starts on demand. No
  server, no Docker, no account.
- **Remote** — an existing/shared FalkorDB (host, port, credentials).

It then registers the Auryn MCP server **user-scoped** with every detected agent (e.g. Claude Code,
via `claude mcp add -s user`) and installs its skill. It writes only to `~/.auryn` and your agent's
own config — **never your repository**.

Non-interactive:

```bash
auryn install -y                                       # auto-select local, no prompts
auryn install -y --backend remote --host db --port 6400  # non-interactive remote
```

Prerequisites: `uv` (or `pipx`/`pip`). Local backend has a wheel for Linux and macOS (Windows via WSL).

---

## Get started (tutorial)

### 1. Install and register

```bash
uv tool install falkordb-auryn
auryn install          # choose "Local", let it register with Claude Code
```

You'll see something like:

```
Backend: local bundled falkordb-lite (starts on demand; no server/Docker)
Registered auryn with claude-code (user scope); skill → ~/.claude/skills/auryn
Restart your agent, then ask a structural question — it indexes the repo on first use.
```

### 2. Open your repo in your agent and ask something structural

```
$ cd ~/work/acme-app && claude
> Who calls chargeCard, and what breaks if I change it?
```

Behind the scenes the agent runs:

```
auryn_status()            # no graph for this repo yet
index_repository(".")          # one-time build (local DB starts automatically)
locate("chargeCard") → id
who_calls(id) ; impact_of(id)  # precise answer, with node ids + file paths
```

The graph is stored centrally (in `~/.auryn`, **not** in your repo). Each repository gets its own
graph automatically, keyed by project directory.

### 3. (Optional) See it

```bash
auryn view          # opens the web graph explorer against the same graph
```

Colour by subsystem, trace a path between two symbols, browse the schema.

### 4. (Optional) Ingest yourself, or merge codebases

```bash
auryn ingest dir .                       # ingest the current directory (one graph per dir)
auryn ingest dir ../api --graph-name platform
auryn ingest dir ../web --graph-name platform   # …merge several repos into one graph
```

### 5. Uninstall cleanly

```bash
auryn uninstall            # deregister from agents, stop the local DB (keeps your graphs)
auryn uninstall --purge    # also delete ~/.auryn
uv tool uninstall falkordb-auryn    # remove the binary
```

---

## Command reference

| Command | What it does |
|---|---|
| `auryn install [-y] [--backend local\|remote]` | Pick a backend, register the MCP server + skill with detected agents |
| `auryn uninstall [--purge]` | Reverse install; `--purge` also deletes `~/.auryn` |
| `auryn ingest dir <path>` | Ingest every supported language under a directory (graph per dir; `--graph-name` to override) |
| `auryn ingest repo <uri> [--ref …]` | Ingest a git repository at a ref |
| `auryn view` | Open the web graph explorer |
| `auryn mcp` | Run the MCP server (this is what your agent launches) |
| `auryn serve` / `stop` | Run/stop the local bundled database explicitly |
| `auryn quickstart <dir>` | Zero-setup one-shot: ingest a dir into an embedded DB and open the viewer |

For the local backend, `view` / `mcp` / `ingest` start the database on demand — nothing is running when
you're not using it; after a reboot the first query cold-starts it (~1s) from the on-disk graph.

---

## MCP tools (what the agent gets)

A curated, bounded, **read-only** tool set over one graph (plus two write tools to build it). Every query
tool goes through `GRAPH.RO_QUERY`, so it's structurally incapable of mutating the graph.

| Tool | Purpose |
|---|---|
| `auryn_status` | Is a graph built for this repo, and what's in it? |
| `index_repository(path=".")` | **(write)** Build the graph for a local directory |
| `ingest_repository(uri, ref)` | **(write)** Build the graph from a git URL |
| `locate(name, kind?)` | Find a symbol by name → its id (every other tool takes the id) |
| `who_calls(id, max_depth?)` | Inward call graph |
| `callees(id, max_depth?)` | Outward call graph |
| `find_path(from_id, to_id, edge_types?)` | Shortest path between two symbols |
| `impact_of(id, direction?)` | Change blast radius across "uses" edges |
| `trace_entrypoint(id)` | Full call-closure of an entry point + tables it touches |
| `get_context(id)` | One node + 1-hop neighbours + source text |
| `get_schema` | Labels, relationship types, counts |
| `run_cypher(query)` | Arbitrary **read-only** Cypher — the escape hatch |

Transport: stdio (the agent launches `auryn mcp` as a subprocess). One server instance = one graph.

---

## Language support

Auto-detected and ingested in a single pass:

| Language | Fast pass | Deep pass |
|---|---|---|
| **Python** | tree-sitter | Jedi (inferred types, receiver-resolved calls) |
| **TypeScript / JavaScript** | tree-sitter (whole JS/TS family, dialect-tagged) | — |
| **C#** | tree-sitter | Roslyn (semantic model + DI resolution; needs .NET SDK) |
| **Java** | tree-sitter | — |
| **C** | tree-sitter | — |
| **PL/SQL** | ANTLR grammar (from source files) | `CALLS`/`READS`/`WRITES` from bodies |
| **SQL** | schema reflection (live DB) + DDL from files | — |

Plus cross-tier seams for **Kendo/ASP.NET MVC views → C# controllers** and **C# → PL/SQL routing**.

---

## Agent integration

`auryn install` auto-detects installed agents and registers the MCP server **user-scoped**, so
every project you open gets its own graph with no per-repo setup. Claude Code is wired via its
`claude mcp add -s user` CLI today; the adapter design is per-agent, so more assistants (Cursor, etc.)
slot in without changing the flow.

Register manually if you prefer:

```bash
claude mcp add -s user auryn -- python -m auryn mcp
```

---

## The graph model (for `get_schema` / a diagram)

Representative labels and relationships (varies by language):

```
(Module|Package|Namespace)-[:HAS_*]->(Function|Class|Method|…)
(Function|Method)-[:CALLS]->(Function|Method)        # resolution: resolved|inferred|unresolved
(Class)-[:EXTENDS]->(Class)   (Class)-[:IMPLEMENTS]->(Interface)
(Method|Field)-[:DEPENDS_ON]->(Type)
(Procedure|Function)-[:READS|WRITES]->(Table)          # WRITES carries INSERT|UPDATE|DELETE
(Method)-[:EXPOSES_ENDPOINT]->(APIEndpoint)
(View)-[:CALLS_API|SUBMITS_TO]->(Method)               # UI → C# seam
(Method)-[:DISPATCHES_TO]->(Procedure|Function)        # C# → PL/SQL seam
```

---

## Running it in a team

Point everyone at one **remote** FalkorDB (`auryn install --backend remote …`) so the graph is
shared, or keep it fully local per-developer. The local backend is a per-user, on-demand database; the
remote backend is a normal shared server.
