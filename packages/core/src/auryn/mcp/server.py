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
import os
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
Code-graph tools over FalkorDB (C, C++, C#, Python, Java, Go, Rust, TS/JS, Ruby, PHP, Kotlin,
Swift, Julia, Bash, PL/SQL). Use them instead of grepping for structural questions: where is X,
who calls X, what does X call, what breaks if X changes, what does this endpoint do.

How to use:
  1. Start with `locate(name=...)` (or `search_code(query=...)` if you don't know the name).
     Results already give `file_path`, `line_start`/`line_end` and `signature`.
  2. Pivot on the returned `id`: `who_calls` / `callees` (each edge carries call `lines` and
     `sites` = {line, code}), `get_context` (compact view; `lines='a-b'` for exact source lines),
     `find_path`, `impact_of`, `trace_entrypoint`.
  3. Cite the file:line evidence these answers give directly. Open a file only for lines the
     answer doesn't show.
If a query returns nothing, `auryn_status` tells you whether the graph has data (and
`index_repository(path=".")` builds it). Check `truncated` before treating a list as complete.
"""

_NOT_FOUND = "no node with id {node_id!r} in graph {graph_name!r} -- call locate() first to find a valid id"

_EMPTY_HINT = (
    "this graph is empty -- call list_graphs() to see the other graphs on this instance, then pass "
    "graph=<name> to any tool (or use_graph(<name>)) to work in one that already holds data"
)
# The same hint when list_graphs/use_graph are hidden by the tool profile (never point an agent at
# a tool it can't see).
_EMPTY_HINT_AGENT = (
    "this graph is empty -- build it with index_repository(path='.'), or pass graph=<name> to query "
    "another graph on this instance"
)

_SNIPPET_CHARS = 280


def _snippet(hit: dict) -> str:
    """A short preview for a search_code hit: the doc if present, else the start of the source,
    trimmed to a preview length. The agent pivots via `id`/`get_context` for the full node."""
    text = (hit.get("doc") or "").strip() or (hit.get("source") or "").strip()
    return text[:_SNIPPET_CHARS]


# Tool profiles (AURYN_MCP_TOOLS). Every advertised tool's definition is sent to the agent on EVERY
# request, so the default exposes only what agents use to answer code questions; the graph-admin
# and escape-hatch tools come back with AURYN_MCP_TOOLS=all.
AGENT_TOOLS = frozenset({
    "auryn_status", "index_repository", "locate", "search_code", "get_context", "who_calls",
    "callees", "find_path", "impact_of", "trace_entrypoint",
})
EXTRA_TOOLS = frozenset({"list_graphs", "use_graph", "ingest_repository", "get_schema", "impact_of_diff", "run_cypher"})
TOOL_PROFILES = {"agent": AGENT_TOOLS, "all": AGENT_TOOLS | EXTRA_TOOLS}
TOOLS_ENV = "AURYN_MCP_TOOLS"


def tool_profile(name: str | None = None) -> frozenset[str]:
    """The tool names a profile exposes: `name`, else $AURYN_MCP_TOOLS, else "agent". An unknown
    profile name raises (a typo must not silently hide or expose tools)."""
    name = (name or os.environ.get(TOOLS_ENV) or "agent").strip().lower()
    if name not in TOOL_PROFILES:
        raise ValueError(f"{TOOLS_ENV}={name!r} is not a tool profile; use one of {sorted(TOOL_PROFILES)}")
    return TOOL_PROFILES[name]


def create_server(client: FalkorDB, graph_name: str, tools: str | None = None, *, embed: str | None = None) -> MCPServer:
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
    exposed = tool_profile(tools)
    graph_listing_hint = ("call list_graphs() to see what's available" if "list_graphs" in exposed
                          else "check the graph name (AURYN_MCP_TOOLS=all adds list_graphs)")
    empty_hint = _EMPTY_HINT if "list_graphs" in exposed else _EMPTY_HINT_AGENT

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
            raise ValueError(f"no graph named {name!r} on this instance -- {graph_listing_hint}")
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
            if fn.__name__ not in exposed:
                return fn  # hidden by the tool profile: defined, never registered/advertised
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
        """Does the graph have data? Returns `has_data`, node counts and the indexed `projects`. Only needed if a query comes back empty. `graph` picks the graph (default: the session default)."""
        g, gname = _read_graph(graph)
        status = gq.graph_status(g)
        out = {"graph": gname, **status}
        if not status.get("has_data"):
            out["hint"] = empty_hint
        return out

    @tool(read_only, "read")
    def list_graphs() -> dict[str, Any]:
        """Every graph on this instance with the repositories each holds; `current` is the session default. Co-locate related repos in one graph so cross-repo calls resolve."""
        return gq.graphs_overview(client, current=state["name"])

    @tool(read_only, "read")
    def use_graph(name: str) -> dict[str, Any]:
        """Set the session default graph to an existing `name` (saves passing `graph=` on every call). Changes nothing in any graph."""
        if name not in graphs and name not in set(client.list_graphs()):
            raise ValueError(
                f"no graph named {name!r} on this instance -- call list_graphs() to see what's available"
            )
        state["name"] = name
        return {"graph": name, "default": True, **gq.graph_status(_handle(name))}

    @tool(writes, "write")
    def index_repository(
        path: str = ".", project_name: str | None = None, deep: str = "inline", graph: str | None = None
    ) -> dict[str, Any]:
        """Build or update the graph for a LOCAL directory (`path`, default '.'); re-running applies only the delta. Use `graph=` to add a related repo to the same graph (cross-repo calls resolve) or a new name to isolate it. `deep`: 'inline' (default), 'background', or 'off'. `graph` picks the graph (default: the session default)."""
        g, gname = _write_graph(graph)
        # Its turn among this repo's hook / watcher syncs, which write the same project subgraph
        # (core/sync_lock.py).
        with contextlib.ExitStack() as turn:
            turn.enter_context(sync_lock(resolve_project_root(path), queue_one=False))
            others = ingest.deep_threads()  # other repos' background deep passes, still running
            try:
                report = ingest.ingest_directory(GraphWriter(g), path, project_name=project_name, deep=deep, embed=embed)
            finally:
                if deep == "background":
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
        deep: str = "inline", graph: str | None = None
    ) -> dict[str, Any]:
        """Like `index_repository` but clones a git `repo_uri` first (`ref` picks a branch/tag/commit). `graph` picks the graph (default: the session default)."""
        g, gname = _write_graph(graph)
        try:
            report = ingest.ingest_repository(GraphWriter(g), repo_uri, ref=ref, project_name=project_name, deep=deep, embed=embed)
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
        """Find symbols by name (case-insensitive substring), optionally by `kind` (Function, Method, Class, ...). Each result has its `id`, `file_path`, `line_start`/`line_end` and `signature` -- cite them directly, and pass the `id` to the other tools. `graph` picks the graph (default: the session default)."""
        g, gname = _read_graph(graph)
        return {"graph": gname, "name": name, "kind": kind, "results": gq.locate_nodes(g, kind, name, limit)}

    @tool(read_only, "read")
    def search_code(query: str, k: int = 10, mode: str = "hybrid", graph: str | None = None) -> dict[str, Any]:
        """Find code by meaning or keywords when you don't know the name (`mode`: hybrid/semantic/lexical). Results carry `id`, `qualified_name` and a `snippet`. Semantic modes need `auryn embed`. `graph` picks the graph (default: the session default)."""
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
                "or fall back to locate()."
            )
        return out

    @tool(read_only, "read")
    def get_context(node_id: str, lines: str | None = None, graph: str | None = None) -> dict[str, Any]:
        """One node, compactly: `file_path`, `line_start`/`line_end`, `signature`, short `doc`, a numbered `preview` of its first lines, and its callers/callees with call sites (file:line + code). Pass `lines='120-160'` to get exactly those numbered source lines instead of opening the file. `graph` picks the graph (default: the session default)."""
        g, gname = _read_graph(graph)
        result = gq.context_view(g, node_id, lines)
        if result is None:
            raise ValueError(_NOT_FOUND.format(node_id=node_id, graph_name=gname))
        return {"graph": gname, **result}

    @tool(read_only, "read")
    def get_schema(graph: str | None = None) -> dict[str, Any]:
        """The graph's labels, edge types, `(From)-[TYPE]->(To)` patterns and property keys -- read before writing `run_cypher`. `graph` picks the graph (default: the session default)."""
        g, gname = _read_graph(graph)
        return {"graph": gname, **gq.graph_schema(g)}

    # -- call graph ----------------------------------------------------------------

    @tool(read_only, "read")
    def who_calls(node_id: str, max_depth: int = gq.DEFAULT_CALL_DEPTH, graph: str | None = None) -> dict[str, Any]:
        """Who calls this function/method, within `max_depth` hops (1 = direct). Each edge gives the caller's `file`, every call `lines` and up to 3 `sites` ({line, code}) -- cite these as file:line evidence. `graph` picks the graph (default: the session default)."""
        g, gname = _read_graph(graph)
        result = gq.callers(g, node_id, max_depth)
        if result is None:
            raise ValueError(_NOT_FOUND.format(node_id=node_id, graph_name=gname))
        return {"graph": gname, **result}

    @tool(read_only, "read")
    def callees(node_id: str, max_depth: int = gq.DEFAULT_CALL_DEPTH, graph: str | None = None) -> dict[str, Any]:
        """What this function/method calls, within `max_depth` hops (1 = direct). Each edge gives the `file`, call `lines` and up to 3 `sites` ({line, code}) in the caller. `graph` picks the graph (default: the session default)."""
        g, gname = _read_graph(graph)
        result = gq.callees(g, node_id, max_depth)
        if result is None:
            raise ValueError(_NOT_FOUND.format(node_id=node_id, graph_name=gname))
        return {"graph": gname, **result}

    @tool(read_only, "read")
    def find_path(
        from_id: str,
        to_id: str,
        max_depth: int = 8,
        directed: bool = True,
        edge_types: list[str] | None = None,
        graph: str | None = None,
    ) -> dict[str, Any]:
        """Shortest path from `from_id` to `to_id` (<= `max_depth` hops) -- does X reach Y, and how. `edge_types=['CALLS']` for a pure call chain; `directed=false` to ignore arrow direction. `graph` picks the graph (default: the session default)."""
        g, gname = _read_graph(graph)
        result = gq.shortest_path(g, from_id, to_id, max_depth, directed, edge_types)
        if result is None:
            raise ValueError("one of the endpoint ids doesn't exist -- call locate() first")
        return {"graph": gname, **result}

    @tool(read_only, "read")
    def impact_of(
        node_id: str,
        direction: str = "downstream",
        max_depth: int = gq.DEFAULT_MAX_DEPTH,
        edge_types: list[str] | None = None,
        limit: int = gq.DEFAULT_TRAVERSAL_LIMIT,
        graph: str | None = None,
    ) -> dict[str, Any]:
        """Blast radius of changing one node over CALLS/DEPENDS_ON/READS/WRITES/REFERENCES. `direction`: 'downstream' (what it could break), 'upstream', or 'both'. `graph` picks the graph (default: the session default)."""
        g, gname = _read_graph(graph)
        result = gq.impact_of(g, node_id, direction, max_depth, edge_types, limit)
        if result is None:
            raise ValueError(_NOT_FOUND.format(node_id=node_id, graph_name=gname))
        return {"graph": gname, **result}

    @tool(read_only, "read", title="What will this change break?")
    def impact_of_diff(
        base: str | None = None,
        head: str | None = None,
        diff: str | None = None,
        max_depth: int = 4,
        graph: str | None = None,
    ) -> dict[str, Any]:
        """Blast radius of a whole diff: a git range (`base`/`head`), raw `diff` text, or (omit all) the uncommitted changes. Returns changed symbols, callers, endpoints and tests reached. `graph` picks the graph (default: the session default)."""
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
        """Everything an entry point (function, method or APIEndpoint) reaches via CALLS, plus tables read/written. Check `truncated`. `graph` picks the graph (default: the session default)."""
        g, gname = _read_graph(graph)
        result = gq.trace_entrypoint(g, entry_id, max_depth, limit)
        if result is None:
            raise ValueError(_NOT_FOUND.format(node_id=entry_id, graph_name=gname))
        return {"graph": gname, **result}

    # -- escape hatch --------------------------------------------------------------

    @tool(read_only, "read")
    def run_cypher(query: str, graph: str | None = None) -> dict[str, Any]:
        """Run any READ-ONLY Cypher query; returns {nodes, edges} plus `columns`/`rows`. Call `get_schema` first. `graph` picks the graph (default: the session default)."""
        g, gname = _read_graph(graph)
        return {"graph": gname, **gq.run_read_query(g, query)}

    return server
