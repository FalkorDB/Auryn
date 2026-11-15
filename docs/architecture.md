# Architecture

How Auryn is put together, for someone reading or extending the codebase.
For the product-level "how it works" pitch, see [`website/architecture.md`](website/architecture.md);
for the graph it produces, see [`graph-model.md`](graph-model.md).

## One paragraph

Auryn turns source (a git repo, a local directory, or a live database) into a
**queryable knowledge graph** in [FalkorDB](https://www.falkordb.com/), and serves that graph
to an AI coding agent through an **MCP server** and to humans through a **web viewer**. Each
language is a self-contained plugin discovered at runtime via entry points. Ingestion is
idempotent: every sync recomputes the target state and writes only the diff.

## The four layers

```
 ┌──────────────────────────────────────────────────────────────────────┐
 │  CLI  (packages/core · auryn.cli)                                      │
 │  run · git-sync · ingest dir|repo · quickstart · view · mcp · install  │
 └───────────────┬──────────────────────────────────────────────────────┘
                 │
 ┌───────────────▼──────────────┐   discovery (entry points)   ┌─────────────────┐
 │  Ingest orchestrator          │◀────────────────────────────│ Language plugins │
 │  core/ingest.py               │   register_git_language()    │ packages/<lang>/ │
 │  auto-detect → per-language   │   register_ingestor()        │ (own dists)      │
 │  sync → enrichment passes     │   register_graph_enrichment()└─────────────────┘
 └───────────────┬──────────────┘
                 │  GraphNode / GraphEdge  →  delta-sync  →  MERGE
 ┌───────────────▼──────────────┐
 │  FalkorDB  (local or remote)  │   real property graph + Cypher
 └───────────────┬──────────────┘
                 │  GRAPH.RO_QUERY (read-only)     ┌──────────────┐
 ┌───────────────▼──────────────┐   shared read    │  MCP server  │→ AI agent
 │  Query layer (graph_queries)  │─────────────────▶│  Web viewer  │→ human
 └──────────────────────────────┘                  └──────────────┘
```

## Plugin discovery (entry points)

Nothing central enumerates the ingestors. Each `auryn-<lang>` distribution declares an
**entry point** in its own `pyproject.toml`, and `core/registry.py` loads them on demand.
Three groups, split so a consumer imports only what it needs:

| Group | Populates | Loaded by | Reached from |
|---|---|---|---|
| `auryn.languages` | `_GIT_REGISTRY` via `register_git_language()` | `discover_git_languages()` | `ingest dir`/`ingest repo`, `ingest_*` MCP tools |
| `auryn.ingestors` | `_REGISTRY` via `@register_ingestor()` | `discover_ingestors()` | `auryn run <name>` |
| `auryn.enrichments` | `_ENRICHMENT_REGISTRY` via `register_graph_enrichment()` | `discover_graph_enrichments()` | after the languages, in the ingest endpoint |

Two properties make discovery cheap:

- **Group isolation.** The multi-language endpoint loads only `languages` + `enrichments`; it
  never imports the heavy live-DB ingestors (Roslyn, ANTLR, SQLAlchemy). The `run <name>` CLI
  loads only `ingestors`.
- **Import-light registration.** A language plugin's entry-point target (`plugin.py`)
  registers a `GitLanguage` whose `sync` is a **lazy thunk** — the actual parser
  (tree-sitter, etc.) isn't imported until a source is found to contain that language's files.

The `auryn.ingestors.*` Python namespace is a [PEP 420](https://peps.python.org/pep-0420/)
namespace package, so every installed plugin distribution merges into it. A lean core-only
install has no plugins at all, and discovery tolerates that (falling back to a bundled
source-tree scan only when *no* entry points are declared — a not-yet-reinstalled dev tree).

## The ingest pipeline

`core/ingest.py` is the orchestrator behind `ingest dir`/`ingest repo` and the `ingest_*` MCP
tools. Given a **source**, it:

1. **Fetches/resolves once.** For a git repo it materializes a bare `GitRepoCache` and
   resolves the ref to a commit sha; for a directory it hashes the tree. This resolved
   source + sha is shared across every language, so no plugin clones or resolves anything.
2. **Auto-detects languages** by scanning for each registered `GitLanguage`'s file suffixes.
3. **Runs each present language's `sync`** (fast pass; then `deep_sync` for two-phase
   languages like Python/C#), collecting a uniform `LanguageIngestResult` per language.
4. **Runs enrichment passes** (`GraphEnrichment.run`) that derive cross-cutting edges from
   the now-complete graph — e.g. the C#→PL/SQL routing seam.
5. **Reports** per-language counts plus any detected-but-unsupported languages.

### The Source abstraction

The whole pipeline consumes its input through a tiny duck-typed surface — essentially
`list_files(sha, suffix, excluded_dirs)` and `read_blob(sha)`. Both `GitRepoCache` (reads git
blobs from a bare clone, no working tree) and `LocalDirectorySource` (walks a folder, keys
files by a content hash) implement it, so **every language ingestor, the view tier, and the
enrichment passes run unchanged over either**. A directory's content hash stands in for a
commit sha, so the same delta machinery applies to both.

## Delta sync (why re-ingesting is cheap)

Ingestion never appends. Each language's `git_sync.py` (see the [Go
reference](adding-a-language.md#10-git_syncpy--the-delta-engine)):

1. `setup_schema(NODE_SCHEMAS)` — create indexes/constraints (idempotent).
2. Short-circuit if the `Project` node's stored `commit_sha` already equals the target.
3. **Recompute** the complete target state for the project at this commit (parse every file →
   resolve → `GraphNode`/`GraphEdge` list). Parsing is cached by git blob sha, so unchanged
   file *content* is never re-parsed regardless of which commit it's under.
4. `read_fingerprints(project_prefix, labels)` — read a cheap hash of every node/edge
   currently under this project.
5. `compute_delta(current, target)` → `apply_delta(delta)` — write only what changed (upsert
   changed, remove gone).
6. `reconcile_linkages(...)` — heal cross-repo placeholder↔definer pairs (ADR 0004).

Because the diff is symmetric, moving to *any* commit/branch/tag (forward, back, sideways) is
the same operation. This is a graph-level diff, not a source diff, on purpose: global
resolution means a change in one file can require relabeling a node declared in an untouched
one. Proven against `dotnet/aspnetcore` (hundreds of thousands of nodes) where a release's
worth of changes re-parses a handful of files. See
[`adr/0002-incremental-resync.md`](adr/0002-incremental-resync.md).

The `BaseIngestor` (live-source) path is simpler: it MERGEs a stream from `extract()` in
batches (`core/base_ingestor.py`), nodes before edges, without a delta step — appropriate for
a full live-DB reflection.

## Storage: FalkorDB, local or remote

FalkorDB is a real property graph with Cypher, not a JSON file or a vector index. Two backends
behind one client (`core/graph_client.py`):

- **Local (embedded).** A bundled, serverless FalkorDB (`falkordblite` — a `redis-server`
  child process with the FalkorDB module, **not** Docker), started lazily on demand and shared
  across sessions via a rendezvous in `~/.auryn`. Powers `quickstart` and the default
  `install`. See [`adr/0001-onboarding-and-server-lifecycle.md`](adr/0001-onboarding-and-server-lifecycle.md).
- **Remote.** An existing/shared FalkorDB (host/port/creds).

Writes go through `GraphWriter` (MERGE, batched, plus the delta primitives:
`read_fingerprints`, `apply_delta`, `node_exists`, `get_property`). Reads for the query layer
go through `graph_queries.py`, which issues **only** `GRAPH.RO_QUERY` — FalkorDB itself rejects
any write clause on that path.

## Serving the graph

- **MCP server** (`mcp/server.py`, stdio transport) — a curated tool set over one graph:
  status, locate, who-calls/callees, find-path, impact, entry-point closure, one-node context,
  schema, raw read-only Cypher, plus the write tools `index_repository`/`ingest_repository`.
  The read tools share `graph_queries.py`, so they're structurally read-only; the two write
  tools are annotated destructive. One server instance binds one graph.
- **Web viewer** (`viewer/app.py`) — a WebGL graph explorer over the same read layer: schema
  overview, node expansion, label/name search, a shortest-path finder, community colouring,
  and git-tracked project sync (the one place the viewer can write — it drives `git-sync`).
  Localhost-only by default (no auth of its own).

## Repository lifecycle

Beyond one-shot ingestion, the tool tracks what it has ingested (`config.toml` registry:
`list`/`forget`), and keeps graphs fresh via `sync` (re-ingest the working dir, touching only
the change's blast radius), git hooks, and a `watch` daemon. See
[`adr/0003-repo-lifecycle-and-resync-triggers.md`](adr/0003-repo-lifecycle-and-resync-triggers.md).

## Where things live

| Path | What |
|---|---|
| `packages/core/src/auryn/core/` | registry, discovery, ingest orchestrator, delta-sync, graph client/queries, source abstractions, lifecycle, install |
| `packages/core/src/auryn/{cli,mcp,viewer}/` | CLI, MCP server, web viewer |
| `packages/<lang>/src/auryn/ingestors/<lang>/` | one language plugin each (see [adding-a-language.md](adding-a-language.md)) |
| `tools/roslyn-extractor/` | the .NET C# semantic extractor |
| `docs/adr/` | architecture decision records (the deep rationale) |
| `demo/` | runnable per-language samples |
| `benchmarks/` | ingest + freshness + agent-adoption benchmark suites |
</content>
