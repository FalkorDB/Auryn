"""MCP server exposing curated, bounded, evidence-backed tools over the FalkorDB
code graphs on one instance -- the "Agent Access Layer" of the Auryn project.

Every *query* tool is read-only by construction (it calls straight into
core/graph_queries.py, which only ever issues Graph.ro_query()); the index tools are
the writes. The server has a DEFAULT graph (chosen at startup via
`auryn mcp --graph-name ...`), but graph selection is per-operation: every tool takes
an optional `graph` argument naming the graph to act on, so one session can query and
index across ALL graphs on the instance without restarting. `use_graph` changes the
default so you don't have to repeat the argument.

The tool set is deliberately shaped for an agent working inside a repository: check
whether a graph exists, index it if not, locate a symbol by name to get its id, then
answer structural questions (who calls X, what does Y call, the path between them, the
change-impact / behavioral closure) without reading whole files -- with `run_cypher` and
`get_schema` as the escape hatch for anything the curated tools don't cover.
"""

from __future__ import annotations

import contextlib
import functools
import threading
import time
from typing import Any

import git
from falkordb import FalkorDB
from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from auryn.core import graph_queries as gq
from auryn.core import hooks
from auryn.core import ingest
from auryn.core import lifecycle
from auryn.core import usage
from auryn.core.embeddings import get_embedder, hybrid_search
from auryn.core.graph_client import GraphWriter
from auryn.core.project import resolve_project_root
from auryn.core.sync_lock import sync_lock

INSTRUCTIONS = """\
Curated tools over the FalkorDB **code graphs** built by the `auryn` project across every
installed language plugin (currently C, C++, C#, Python, Java, Go, Rust, TypeScript/JavaScript,
Ruby, PHP, Kotlin, Swift, Julia, Bash, and PL/SQL). Prefer these over reading/grepping files
for structural questions --
"where is X", "who calls X", "what does X call", "what breaks if X changes", "trace a
request into the code" -- because one query returns the precise answer with ids instead
of pulling whole files into context.

Typical workflow:
  1. `auryn_status` -- is there a graph for this repo yet, and what's in it?
  2. If empty, `index_repository(path=".")` to build it (local dir; `ingest_repository`
     for a git URL). Indexing is a one-time cost; queries afterward are cheap. Deep analysis
     (precise call resolution -- trait/generic dispatch, cross-module/crate, untyped receivers)
     runs ASYNC by default: the graph is queryable immediately but keeps DEEPENING in the
     background. While it does, `auryn_status` and the call/impact tools (who_calls, callees,
     impact_of, find_path, trace_entrypoint) carry a `deepening` note -- so if precise call edges
     matter, re-query or poll `auryn_status` until its `depth` is "deep".
  3. Find your starting symbol, then pivot on the `id` it returns (every other tool takes
     the internal `id`, never a display name):
       - `locate(name=...)` when you know the name (case-insensitive substring).
       - `search_code(query=...)` when you DON'T -- semantic/vector search by meaning
         ("where do we validate eligibility"). Requires embeddings (`auryn embed`).

WORKING ACROSS GRAPHS. One instance holds many graphs, and a graph can hold several repos.
Every tool takes an optional `graph=<name>` -- omit it to use the session default, or pass a
name to act on any other graph WITHOUT restarting the server. `list_graphs` shows every graph
(and the repos each holds) so you can pick one; `use_graph(<name>)` changes the default so you
don't have to repeat `graph=` on every call. Co-locate repos that belong to one system in a
single graph (that's what resolves cross-repo calls into real edges) by indexing them with the
same `graph=`; give an unrelated repo its own graph name.
  4. Answer the question with the smallest tool that fits: `who_calls`, `callees`,
     `find_path` (does X reach Y, and how -- any edge type, or `edge_types=["CALLS"]`
     for a call chain), `get_context` (1-hop + source), `impact_of` (change blast
     radius), `trace_entrypoint` (full call-closure of an entry point end-to-end, incl.
     web endpoints).
  5. `get_schema` (labels/edge-types/counts) and `run_cypher` (arbitrary READ-ONLY
     Cypher) are the escape hatch for anything custom.

Model notes: nodes are keyed by an internal `id`. Python endpoints (APIEndpoint +
EXPOSES_ENDPOINT, from route decorators) and C# endpoints are both entry points for
`trace_entrypoint`. Line numbers are present on C# (and where a parser propagated
them); some ingestors carry a file path and/or full `source` text instead. Check
`truncated` on traversal results before treating them as complete.
"""

_NOT_FOUND = "no node with id {node_id!r} in graph {graph_name!r} -- call locate() first to find a valid id"

_EMPTY_HINT = (
    "this graph is empty -- call list_graphs() to see the other graphs on this instance, then pass "
    "graph=<name> to any tool (or use_graph(<name>)) to work in one that already holds data"
)

_SNIPPET_CHARS = 280


def _snippet(hit: dict) -> str:
    """A short preview for a search_code hit: the doc if present, else the start of the source,
    trimmed to a preview length. The agent pivots via `id`/`get_context` for the full node."""
    text = (hit.get("doc") or "").strip() or (hit.get("source") or "").strip()
    return text[:_SNIPPET_CHARS]


def create_server(client: FalkorDB, graph_name: str, *, embed: str | None = None) -> MCPServer:
    """Builds the MCP server with a DEFAULT binding to `graph_name` on `client`.

    Graph selection is per-operation: every tool takes an optional `graph` argument naming the
    graph to act on, so one session can query and index across ALL graphs on the instance. `graph`
    omitted -> the session default (initially `graph_name`, changeable with `use_graph`). Graph
    handles are created lazily and cached (a Graph is a thin wrapper, so this just avoids rebuilding
    it per call).

    `embed` overrides the post-ingest embedding mode for this server's index/ingest tools (see
    ingest._maybe_embed). The CLI passes "inline" when the server was started against an explicit
    `--host` -- an ad-hoc connection the background embed daemon (which reconnects via the
    *configured* backend) couldn't reach, so those ingests must embed inline."""
    default_name = graph_name
    graphs: dict[str, Any] = {default_name: client.select_graph(default_name)}
    state = {"name": default_name}  # the session default; use_graph mutates it

    server = MCPServer(name="auryn", instructions=INSTRUCTIONS, version="0.2.0")

    read_only = ToolAnnotations(read_only_hint=True)
    writes = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True)

    def _handle(name: str):
        g = graphs.get(name)
        if g is None:
            g = graphs[name] = client.select_graph(name)
        return g

    def _read_graph(name: str | None) -> tuple[Any, str]:
        """The graph a READ acts on: the session default, or an explicit graph that must already
        exist -- an unknown name raises (a typo can't masquerade as an empty graph)."""
        if name is None:
            name = state["name"]
        elif name != state["name"] and name not in graphs and name not in set(client.list_graphs()):
            raise ValueError(
                f"no graph named {name!r} on this instance -- call list_graphs() to see what's available"
            )
        return _handle(name), name

    def _write_graph(name: str | None) -> tuple[Any, str]:
        """The graph a WRITE acts on. Unlike a read, an unknown name is allowed -- indexing into it
        CREATES it, so an agent can build an isolated graph without launching a separate server."""
        name = name or state["name"]
        return _handle(name), name

    def _cached_embedder():
        """The query embedder, loaded once per server process (the model load is expensive) and
        reused across search_code calls. Construction is lazy (no model download until embed)."""
        if "embedder" not in state:
            state["embedder"] = get_embedder()
        return state["embedder"]

    def _deepening(g) -> dict | None:
        """A note when the graph is still DEEPENING: any Project at depth="fast" has a background/async
        deep pass (jedi / Roslyn / rust-analyzer) that hasn't landed yet, so its precise call edges
        (trait/generic dispatch, untyped-receiver and cross-module/crate calls) may be missing. Returns
        None once every two-phase project is "deep" (single-phase projects set no depth, so they never
        trip this). With deep now async by default, this is what tells an agent a who_calls/impact result
        might be incomplete -- so it can re-query or wait."""
        try:
            rows = g.ro_query("MATCH (p:Project) WHERE p.depth = 'fast' RETURN DISTINCT p.name").result_set
        except Exception:
            return None  # a malformed/empty graph must never break a read
        pending = sorted({str(r[0]) for r in rows if r and r[0] is not None})
        if not pending:
            return None
        return {"depth": "fast", "projects": pending,
                "note": "this graph is still deepening in the background; call resolution is currently "
                        "name-based, so deep-only edges (trait/generic dispatch, untyped-receiver and "
                        "cross-module/crate calls) may be missing. Re-query shortly, or poll auryn_status "
                        "until depth is 'deep'."}

    def _with_deepening(g, out: dict) -> dict:
        """Annotate a call-resolution-dependent result with the deepening note, when applicable."""
        note = _deepening(g)
        if note is not None:
            out["deepening"] = note
        return out

    def tool(annotations: ToolAnnotations, op: str, **tool_kwargs):
        """Register a tool AND record each call as a usage event (core/usage.py) -- so every graph
        access is counted by operation type. `tool_kwargs` (e.g. `title=`) pass through to the MCP
        registration. Defined here (not in usage.py) so functools.wraps + the MCP server's
        signature/return-hint introspection resolve against this module's globals
        (`from __future__ import annotations` makes the tool hints strings). Recording is
        best-effort and never affects the tool's result. The recorded graph is the one the call
        actually acted on (from the result's `graph` key), not necessarily the session default."""
        register = server.tool(annotations=annotations, **tool_kwargs)

        def decorate(fn):
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                start = time.monotonic()
                status, error, result = "ok", None, None
                try:
                    result = fn(*args, **kwargs)
                    return result
                except Exception as e:
                    status, error = "error", f"{type(e).__name__}: {e}"
                    raise
                finally:
                    eff = result["graph"] if isinstance(result, dict) and isinstance(result.get("graph"), str) else state["name"]
                    usage.record_call(
                        graph=eff, tool=fn.__name__, op=op, status=status,
                        duration_ms=(time.monotonic() - start) * 1000, result=result, error=error,
                    )

            return register(wrapper)

        return decorate

    # -- discovery / indexing ------------------------------------------------------

    @tool(read_only, "read")
    def auryn_status(graph: str | None = None) -> dict[str, Any]:
        """Is there a code graph yet? Returns `has_data`, total `node_count`, per-label counts, and
        the git-tracked `projects` already indexed. Call this first -- if `has_data` is false, index
        the repo (or switch to a graph that has data). `graph` picks which graph to report on
        (default: the session default); `list_graphs` shows every graph on the instance."""
        g, gname = _read_graph(graph)
        status = gq.graph_status(g)
        out = {"graph": gname, **status}
        if not status.get("has_data"):
            out["hint"] = _EMPTY_HINT
        return _with_deepening(g, out)  # surface whether a background deep pass is still landing

    @tool(read_only, "read")
    def list_graphs() -> dict[str, Any]:
        """Every code graph on this FalkorDB instance and the repositories each one REPRESENTS
        -- a graph can hold a single repo or several indexed together. `current` marks the session
        default (the graph tools use when you omit `graph=`); pass `graph=<name>` to any tool, or
        `use_graph(<name>)`, to act on a different one. Per graph you get its `name`, `node_count`,
        and `repositories` (each `{name, repo_uri, depth, base_commit}`).

        Read this to decide where a new repo belongs: co-locating related repos in one graph (index
        them with the same `graph=`) is what lets cross-repo calls resolve into real edges; an
        unrelated repo is better off under its own graph name."""
        return gq.graphs_overview(client, current=state["name"])

    @tool(read_only, "read")
    def use_graph(name: str) -> dict[str, Any]:
        """Change the session DEFAULT graph to `name` (must already exist on this instance) so
        subsequent tools act on it without an explicit `graph=` argument. This is a convenience over
        passing `graph=` every call -- it changes nothing in any graph. Returns the new default's
        status. Call `list_graphs` to see the available names."""
        if name not in graphs and name not in set(client.list_graphs()):
            raise ValueError(
                f"no graph named {name!r} on this instance -- call list_graphs() to see what's available"
            )
        state["name"] = name
        return {"graph": name, "default": True, **gq.graph_status(_handle(name))}

    @tool(writes, "write")
    def index_repository(
        path: str = ".", project_name: str | None = None, deep: str = "async", graph: str | None = None
    ) -> dict[str, Any]:
        """Build (or update) the code graph for a LOCAL repository directory -- no git
        required. Auto-detects every installed language plugin (currently C, C++, C#, Python,
        Java, Go, Rust, TypeScript/JavaScript, Ruby, PHP, Kotlin, Swift, Julia, Bash, PL/SQL)
        and ingests all their files, returning a per-language report. Re-running derives a precise
        delta (unchanged = no-op, changed file = minimal update). `project_name` defaults to
        the directory name. This is the one-time setup an agent does before querying.

        `graph` picks the target graph (default: the session default). Indexing an additional repo
        into the SAME graph ADDS it alongside the existing ones -- do that when the repos belong to
        one system (they call each other / share a runtime): co-locating them is exactly what makes
        cross-repo references resolve into real CALLS/impact edges. For an UNRELATED repo, pass a
        NEW `graph=<name>` -- an unknown name is created on write, so you get a dedicated, scoped
        graph without launching a separate server. Call `list_graphs` first to see what exists.

        `deep` controls the second, deeper pass for the languages that have one (Python via
        Jedi, C# via Roslyn, Rust via rust-analyzer) -- a fast structural pass first, then inferred
        types + resolved calls:
          - "async" (default): return as soon as the fast graph is ready and deepen it in a detached
            daemon -- you can query immediately; types/call edges land shortly after. The graph
            reports its state per project as `depth` = "fast" then "deep", so check it before relying
            on deep-only results (precise call resolution).
          - "inline": return only when the graph is fully deep. Use it when you must not query until
            resolution is complete.
          - "off": fast graph only (shallow types).
        (An ad-hoc/embedded connection a detached daemon can't reach falls back to inline.)

        Same effect as the CLI `ingest`: after building the graph this registers the repo and
        sets up whatever resync trigger you chose at install time (`commit` -> git hooks, `save`
        -> folder watcher, `manual` -> nothing), reported back under `resync`. So a repo an agent
        indexes stays fresh on later commits/saves exactly as one indexed from the CLI would."""
        g, gname = _write_graph(graph)
        # Its turn among this repo's hook / watcher syncs, which write the same project subgraph
        # (core/sync_lock.py).
        with contextlib.ExitStack() as turn:
            turn.enter_context(sync_lock(resolve_project_root(path), queue_one=False))
            others = ingest.deep_threads()  # other repos' background deep passes, still running
            # An ad-hoc (`--host`) server connection is one a detached daemon may not match (embed is
            # forced inline for the same reason) -> deepen inline rather than queue a job it can't reach.
            _deep = "inline" if deep == "async" and embed == "inline" else deep
            try:
                report = ingest.ingest_directory(GraphWriter(g), path, project_name=project_name, deep=_deep, embed=embed)
            finally:
                if _deep == "background":
                    # That pass still writes the subgraph after this call has answered: a thread
                    # takes the lock over, waits for this call's pass (not the others') and only
                    # then releases it.
                    for thread in ingest.deep_threads():
                        if thread not in others:
                            turn.callback(thread.join)
                    threading.Thread(target=turn.pop_all().close, name="unlock-after-deep", daemon=True).start()
        # Shared with the CLI (lifecycle.wire_trigger) so the MCP can't skip the registry/hook
        # setup the CLI does -- honours the install-time [resync].trigger choice.
        resync = lifecycle.wire_trigger(str(resolve_project_root(path)), gname, project_name)
        return {"graph": gname, "resync": resync, **report.to_dict()}

    @tool(writes, "write")
    def ingest_repository(
        repo_uri: str, ref: str | None = None, project_name: str | None = None,
        deep: str = "async", graph: str | None = None
    ) -> dict[str, Any]:
        """Like `index_repository`, but clones a GIT repository first (`repo_uri` is a path or
        any URI git understands; `ref` picks a branch/tag/commit). Use this for a remote repo;
        use `index_repository` for the working directory you're already in. `deep`
        ("async" default | "inline" | "off") works exactly as in `index_repository`.

        The same graph selection applies: `graph` targets an existing graph to EXTEND (co-locate a
        related repo so cross-repo references resolve) or a NEW name to ISOLATE (created on write).
        Call `list_graphs` first.

        Like `index_repository`, it then wires the install-time resync trigger and reports it
        under `resync` -- but only a LOCAL path has a working tree to attach to; a remote clone
        has none, so `resync` will note that (clone locally to get commit/save resync)."""
        g, gname = _write_graph(graph)
        _deep = "inline" if deep == "async" and embed == "inline" else deep  # ad-hoc -> daemon can't reach
        try:
            report = ingest.ingest_repository(GraphWriter(g), repo_uri, ref=ref, project_name=project_name, deep=_deep, embed=embed)
        except git.GitCommandError as e:
            raise ValueError(f"couldn't access git repository {repo_uri!r}: {e}") from e
        # Parity with the CLI: wire the resync trigger onto a local checkout when the URI is one;
        # a remote clone-to-cache has no working tree to attach hooks/watcher to.
        local_dir = str(resolve_project_root(repo_uri)) if hooks.is_git_repo(repo_uri) else None
        resync = lifecycle.wire_trigger(local_dir, gname, project_name)
        return {"graph": gname, "resync": resync, **report.to_dict()}

    # -- lookup --------------------------------------------------------------------

    @tool(read_only, "read")
    def locate(name: str, kind: str | None = None, limit: int = 20, graph: str | None = None) -> dict[str, Any]:
        """Find symbols by name (case-insensitive substring), optionally filtered by `kind`
        (a node label: "Function", "Method", "Class", "Module", "APIEndpoint", "Table", ...).
        Returns each match's `id`, `label`, `name`, and properties -- feed a result's `id`
        into the other tools. This is name search, not semantic/vector search. `graph` picks which
        graph to search (default: the session default)."""
        g, gname = _read_graph(graph)
        return {"graph": gname, "name": name, "kind": kind, "results": gq.search_nodes(g, kind, name, limit)}

    @tool(read_only, "read")
    def search_code(query: str, k: int = 10, mode: str = "hybrid", graph: str | None = None) -> dict[str, Any]:
        """Find code by MEANING and/or NAME -- the retrieval complement to `locate`. Describe what
        you want in natural language ("where do we validate user eligibility", "retry logic for
        database connections"); results are the nearest callables/types.

        `mode`:
          - "hybrid" (default) -- fuse a lexical (BM25 over name+doc) and a vector (semantic)
            ranker via Reciprocal Rank Fusion. Best overall: keyword precision + meaning recall.
          - "semantic" -- vector only (pure meaning).
          - "lexical" -- full-text only (keywords; needs no embedding model).

        Each result has `id` (feed into `get_context`/`who_calls`/`callees` to pivot into the
        structure), `kind`, `name`, `qualified_name`, a `snippet`, and `score` -- results are
        already ordered best-first, so prefer order over the raw score (its scale differs by
        mode). Use this as the ENTRY POINT when you don't know the symbol's name; use `locate`
        when you do. `graph` picks which graph to search (default: the session default).

        Needs an embed pass (`auryn embed`) for hybrid/semantic. Degrades gracefully: hybrid
        falls back to lexical if the model is unavailable and to vector if there's no full-text
        index; empty `results` come with a `hint`."""
        g, gname = _read_graph(graph)
        writer = GraphWriter(g)
        rp = ("name", "qualified_name", "doc", "source")
        mode = (mode or "hybrid").lower()
        try:
            if mode == "lexical":
                hits = writer.fulltext_search(query, k, return_properties=rp)
            elif mode == "semantic":
                hits = writer.knn_search(_cached_embedder().embed_query(query), k, return_properties=rp)
            else:  # hybrid (default) -- degrades to lexical/vector internally if one ranker is down
                mode = "hybrid"
                try:
                    embedder = _cached_embedder()
                except Exception:  # model can't even be constructed -> lexical-only hybrid
                    embedder = None
                hits = hybrid_search(writer, embedder, query, k, return_properties=rp)
        except Exception as e:  # e.g. semantic mode with the model unavailable -> degrade, don't crash
            return {
                "graph": gname, "query": query, "mode": mode, "results": [],
                "hint": f"search unavailable: {type(e).__name__}: {e}. Try mode='lexical', or locate().",
            }
        results = [
            {
                "id": h["id"], "kind": h.get("kind"),
                "name": h.get("name"), "qualified_name": h.get("qualified_name"),
                "score": h.get("score"), "snippet": _snippet(h),
            }
            for h in hits
        ]
        out = {"graph": gname, "query": query, "mode": mode, "results": results}
        if not results:
            out["hint"] = (
                "no results -- build the index with `auryn embed` (hybrid/semantic need it), "
                "or fall back to locate()/get_schema()."
            )
        return out

    @tool(read_only, "read")
    def get_context(node_id: str, graph: str | None = None) -> dict[str, Any]:
        """A bounded context package for one node: its own properties (incl. `source` text
        where captured) plus its immediate 1-hop neighbours in both directions. Sized for one
        LLM context window -- use the traversal tools for anything deeper. `graph` picks which
        graph the id lives in (default: the session default)."""
        g, gname = _read_graph(graph)
        result = gq.neighbors(g, node_id)
        if result is None:
            raise ValueError(_NOT_FOUND.format(node_id=node_id, graph_name=gname))
        return {"graph": gname, **result}

    @tool(read_only, "read")
    def get_schema(graph: str | None = None) -> dict[str, Any]:
        """The graph's META-MODEL, so you understand its STRUCTURE before querying: node
        labels (with counts), edge types, the actual `(FromLabel)-[TYPE]->(ToLabel)` patterns
        present (the schema graph -- e.g. `(Method)-[CALLS]->(Method)`,
        `(Class)-[EXTENDS]->(Class)`), and the property keys each label carries. Read this
        first to know which traversal answers a question, or to write a correct `run_cypher`.
        `graph` picks which graph to describe (default: the session default)."""
        g, gname = _read_graph(graph)
        return {"graph": gname, **gq.graph_schema(g)}

    # -- call graph ----------------------------------------------------------------

    @tool(read_only, "read")
    def who_calls(node_id: str, max_depth: int = gq.DEFAULT_CALL_DEPTH, graph: str | None = None) -> dict[str, Any]:
        """Who CALLS this function/method (inward), within `max_depth` hops (1 = direct
        callers). The answer to "what uses X / who depends on X's behavior". `graph` picks which
        graph the id lives in (default: the session default)."""
        g, gname = _read_graph(graph)
        result = gq.callers(g, node_id, max_depth)
        if result is None:
            raise ValueError(_NOT_FOUND.format(node_id=node_id, graph_name=gname))
        return _with_deepening(g, {"graph": gname, **result})

    @tool(read_only, "read")
    def callees(node_id: str, max_depth: int = gq.DEFAULT_CALL_DEPTH, graph: str | None = None) -> dict[str, Any]:
        """What this function/method CALLS (outward), within `max_depth` hops (1 = direct
        callees). The answer to "what does X do / what does X depend on". `graph` picks which
        graph the id lives in (default: the session default)."""
        g, gname = _read_graph(graph)
        result = gq.callees(g, node_id, max_depth)
        if result is None:
            raise ValueError(_NOT_FOUND.format(node_id=node_id, graph_name=gname))
        return _with_deepening(g, {"graph": gname, **result})

    @tool(read_only, "read")
    def find_path(
        from_id: str,
        to_id: str,
        max_depth: int = 8,
        directed: bool = True,
        edge_types: list[str] | None = None,
        graph: str | None = None,
    ) -> dict[str, Any]:
        """The shortest path between two nodes (<= `max_depth` hops) -- "does X reach Y, and
        how". `edge_types` scopes which relationships to traverse: omit it to connect them
        over ANY edge type (e.g. a Method to a Table, a Class to an APIEndpoint), or pass
        e.g. `["CALLS"]` for a pure call chain. `directed` follows edge direction
        from_id->...->to_id; set it false to connect them regardless of which way the arrows
        point (more likely to find something). Returns the ordered `nodes` and the connecting
        `edges` (each with its real direction and type); `found` is false if no path exists
        within the bound. `graph` picks which graph the ids live in (default: the session default)."""
        g, gname = _read_graph(graph)
        result = gq.shortest_path(g, from_id, to_id, max_depth, directed, edge_types)
        if result is None:
            raise ValueError("one of the endpoint ids doesn't exist -- call locate() first")
        return _with_deepening(g, {"graph": gname, **result})

    @tool(read_only, "read")
    def impact_of(
        node_id: str,
        direction: str = "downstream",
        max_depth: int = gq.DEFAULT_MAX_DEPTH,
        edge_types: list[str] | None = None,
        limit: int = gq.DEFAULT_TRAVERSAL_LIMIT,
        graph: str | None = None,
    ) -> dict[str, Any]:
        """Change-impact traversal from one node over "uses" edges (default CALLS/DEPENDS_ON/
        READS/WRITES/REFERENCES), up to `max_depth` hops (1-10). `direction`: "downstream"
        (what this could break), "upstream" (what could force this to change), or "both".
        Broader than who_calls/callees (multiple edge types); use it for "blast radius".
        `graph` picks which graph the id lives in (default: the session default)."""
        g, gname = _read_graph(graph)
        result = gq.impact_of(g, node_id, direction, max_depth, edge_types, limit)
        if result is None:
            raise ValueError(_NOT_FOUND.format(node_id=node_id, graph_name=gname))
        return _with_deepening(g, {"graph": gname, **result})

    @tool(read_only, "read", title="What will this change break?")
    def impact_of_diff(
        base: str | None = None,
        head: str | None = None,
        diff: str | None = None,
        max_depth: int = 4,
        graph: str | None = None,
    ) -> dict[str, Any]:
        """"What will this change break?" -- the blast radius of a DIFF (a whole PR/commit at
        once), not a single node. Provide a git range (`base`, optionally `head`), OR raw
        unified-diff text in `diff`, OR omit all three to use the working tree's uncommitted
        changes. Maps the changed LINES to graph symbols, then returns the reverse-dependency
        closure: `changed_symbols`, and under `impact` the `direct_callers` + `transitive`
        dependents, `api_endpoints_reached`, `tests_covering`, `cross_service_consumers` (other
        repos co-located in this graph), and `affected` (every dependent with its node `id`, to
        pivot via who_calls/get_context). `flags` calls out cross-service breaks and missing test
        coverage; `unmapped_files` are changed files with no graph symbols (config/docs). Use this
        in code review before merging. `graph` picks which graph to resolve against."""
        from auryn.core import impact as impact_mod

        g, gname = _read_graph(graph)
        if diff is None:
            repo = str(resolve_project_root())
            try:
                diff = impact_mod.git_diff(repo, base, head)
            except Exception as e:
                return {"graph": gname, "error": f"couldn't compute git diff in {repo}: {type(e).__name__}: {e}"}
        changed, _deleted = impact_mod.parse_unified_diff(diff)
        if not changed:
            return {"graph": gname, "base": base, "head": head,
                    "changed_symbols": [], "impact": {"total_affected": 0},
                    "hint": "empty diff -- nothing changed in the given range."}
        report = impact_mod.impact_of_diff(g, changed, max_depth=max_depth)
        return {"graph": gname, "base": base, "head": head, **report}

    @tool(read_only, "read", title="Trace an entry point end-to-end")
    def trace_entrypoint(
        entry_id: str, max_depth: int | None = None, limit: int = gq.DEFAULT_TRAVERSAL_LIMIT,
        graph: str | None = None,
    ) -> dict[str, Any]:
        """What does this entry point actually do, end to end? Returns its full call-closure --
        every function reachable via CALLS -- plus any tables those functions read or write. Pass
        a function/method, a Procedure/Function, or an APIEndpoint (an endpoint resolves to its
        handler first, so "what does this route do end-to-end" works). Unbounded by default
        (completeness is the point); check `truncated`. `graph` picks which graph the id lives in
        (default: the session default)."""
        g, gname = _read_graph(graph)
        result = gq.trace_entrypoint(g, entry_id, max_depth, limit)
        if result is None:
            raise ValueError(_NOT_FOUND.format(node_id=entry_id, graph_name=gname))
        return _with_deepening(g, {"graph": gname, **result})

    # -- escape hatch --------------------------------------------------------------

    @tool(read_only, "read")
    def run_cypher(query: str, graph: str | None = None) -> dict[str, Any]:
        """Run an arbitrary READ-ONLY Cypher query (FalkorDB rejects any write clause). For
        questions the curated tools don't cover. Call `get_schema` first to see the labels and
        edge types. Returns matched graph entities as {nodes, edges} for a graph view, AND
        `columns`/`rows` -- the tabular result set -- so projections and aggregations that aren't
        graph entities (`RETURN e.route, count(*)`) come back too. `graph` picks which graph to
        query (default: the session default)."""
        g, gname = _read_graph(graph)
        return {"graph": gname, **gq.run_read_query(g, query)}

    return server
