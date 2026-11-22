"""Flask app serving the graph viewer UI and its read-only JSON API, plus exactly
one write-capable endpoint (git-sync). Every other endpoint here never writes to
the graph -- most only ever issue GRAPH.RO_QUERY (via core/graph_queries.py, shared
with the MCP server); git-refs is the one exception that does real I/O (a git
fetch, to list current branches/tags) but still never touches FalkorDB."""

from __future__ import annotations

import ipaddress
import os
import socket

import git
from falkordb import FalkorDB
from flask import Flask, abort, jsonify, request

from auryn.core import graph_queries as queries
from auryn.core.embeddings import hybrid_search
from auryn.core.graph_client import GraphWriter


def _loopback_ip(address: str) -> bool:
    """Whether `address` is a loopback IP. An IPv4-mapped one (`::ffff:127.0.0.1`) is judged by its
    IPv4 form, which the `ipaddress` of older Python 3.12 releases doesn't do. Raises ValueError for a
    name that isn't an IP."""
    ip = ipaddress.ip_address(address)
    return (getattr(ip, "ipv4_mapped", None) or ip).is_loopback


def _is_loopback(name: str) -> bool:
    """`localhost`, or a loopback address in any spelling (all of 127.0.0.0/8, `::1`,
    `0:0:0:0:0:0:0:1`, `::ffff:127.0.0.1`) -- as a bind address or as the name in a Host header."""
    name = name.strip("[]").lower()
    if name == "localhost":
        return True
    try:
        return _loopback_ip(name)
    except ValueError:  # some other DNS name
        return False


def _host_name(host: str) -> str:
    """The name in a Host header -- `name`, `name:port`, `[v6]` or `[v6]:port` -- without its port
    or brackets, in lower case, and without a trailing dot (`localhost.` is `localhost`)."""
    name = host[: host.index("]") + 1] if host.startswith("[") and "]" in host else host.rsplit(":", 1)[0]
    return name.strip("[]").lower().removesuffix(".")


def _binds_loopback(host: str) -> bool:
    """Whether binding to `host` can land on loopback, resolved the way the OS resolves a bind
    address -- so `127.1`, `2130706433`, `0x7f.1` or a hosts-file alias count, not only the
    canonical spellings. Fails closed: a name that does not resolve keeps the Host check on."""
    if _is_loopback(host):
        return True
    try:
        infos = socket.getaddrinfo(host.strip("[]"), None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return True
    for *_, sockaddr in infos:
        try:
            if _loopback_ip(str(sockaddr[0]).split("%", 1)[0]):
                return True
        except ValueError:
            return True
    return False


def _sync_blocker(project: dict) -> str | None:
    """Why the viewer can't git-sync this Project node, or None when it can. The Sources tab's
    Sync is the C# git-sync (see graph_git_sync), so it fits a C# project tracked in a git
    repository. Not a project indexed from a plain folder -- what `auryn quickstart` shows first:
    there is no branch or tag to move it to -- and not another language's, which the C# sync
    would quietly ingest a second time, as C#."""
    uri = project["properties"].get("repo_uri") or ""
    if not uri:
        return "No source is recorded for this project, so there is nothing to sync from."
    # Only an ABSOLUTE local path can be judged from here. A relative one (`auryn ingest repo .`
    # stores it as given) was relative to where the ingest ran, not to this viewer; a remote URI
    # is git's to open.
    path = os.path.expanduser(uri)
    if os.path.isabs(path):
        try:
            with git.Repo(path):
                pass
        except git.InvalidGitRepositoryError:  # a folder that exists but isn't a repository's root
            return ("Indexed from a folder that isn't the root of a git repository: there is no "
                    "branch or tag to sync to. Re-ingest the folder to refresh the graph.")
        except git.NoSuchPathError:  # it was there when the graph was built
            return "The source folder no longer exists, so there is nothing to sync from. Re-ingest it from where it is now."
        except Exception:  # noqa: BLE001 -- unreadable, malformed: the sync itself will tell
            pass
    if not str(project["id"]).startswith("csharp://"):
        return "Sync from the viewer handles C# projects only. Re-ingest this one to refresh the graph."
    return None


def create_app(client: FalkorDB, host: str = "127.0.0.1") -> Flask:
    """`host` is the address the server is bound to (the `--host` of `view` / `quickstart`)."""
    app = Flask(__name__, static_folder="static", static_url_path="")

    if _binds_loopback(host):
        bind_name = host.strip("[]").lower().removesuffix(".")  # a bind address has no port
        # Bound to loopback the viewer is this machine's only -- but a web page can still point its
        # OWN name at 127.0.0.1 (DNS rebinding) and then read every graph, or POST git-sync, as
        # same-origin. Such a request carries the page's name in Host, so serve loopback names only.
        # Bound anywhere else (`--host 0.0.0.0`) it was exposed on purpose: no check.
        @app.before_request
        def _loopback_host_only():
            name = _host_name(request.host)
            # The bound name itself is fine too (e.g. a hosts-file alias the user chose).
            if not _is_loopback(name) and name != bind_name:
                abort(403, "Host not allowed: bound to loopback, the viewer answers loopback names "
                           "(localhost, 127.0.0.1) and the name it was started with (--host).")

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.get("/api/graphs")
    def list_graphs():
        return jsonify(client.list_graphs())

    @app.get("/api/graphs/<graph_name>/overview")
    def graph_overview(graph_name: str):
        graph = client.select_graph(graph_name)
        return jsonify(queries.overview(graph))

    @app.get("/api/graphs/<graph_name>/roots")
    def graph_roots(graph_name: str):
        graph = client.select_graph(graph_name)
        return jsonify(queries.root_nodes(graph))

    @app.get("/api/graphs/<graph_name>/search")
    def graph_search(graph_name: str):
        graph = client.select_graph(graph_name)
        label = request.args.get("label") or None
        q = request.args.get("q") or None
        limit = request.args.get("limit", type=int) or queries.DEFAULT_SEARCH_LIMIT
        return jsonify(queries.search_nodes(graph, label, q, limit))

    _embedder_cache: dict = {}

    def _embedder():
        """The query embedder, loaded once per viewer process (the model download/load is
        expensive). Uses the configured model so it matches whatever the graph was embedded with."""
        if "e" not in _embedder_cache:
            from auryn.core.embeddings import configured_model, get_embedder

            _embedder_cache["e"] = get_embedder(configured_model())
        return _embedder_cache["e"]

    @app.get("/api/graphs/<graph_name>/search-semantic")
    def graph_search_semantic(graph_name: str):
        """Hybrid search: fuse a lexical (BM25 over name+doc) and a vector (semantic) ranker via
        RRF (see core/embeddings/search). Returns nodes in the /search shape (`id`, `label`,
        `name`) plus a fused `score` (higher is better), best-first. Degrades gracefully -- if the
        embedding model can't load it returns lexical-only results; empty list when neither the
        full-text nor the vector index exists yet."""
        graph = client.select_graph(graph_name)
        q = request.args.get("q") or None
        k = request.args.get("k", type=int) or queries.DEFAULT_SEARCH_LIMIT
        if not q:
            return jsonify([])
        hits = hybrid_search(
            GraphWriter(graph), _embedder(), q, k, return_properties=("name", "qualified_name")
        )
        return jsonify(
            [
                {
                    "id": h["id"],
                    "label": h.get("kind"),
                    "name": h.get("name") or h.get("qualified_name") or h["id"],
                    "score": round(h["score"], 4),
                }
                for h in hits
            ]
        )

    @app.get("/api/graphs/<graph_name>/node")
    def graph_node(graph_name: str):
        graph = client.select_graph(graph_name)
        node_id = request.args.get("id")
        if not node_id:
            return jsonify({"error": "missing 'id' query parameter"}), 400
        node = queries.node_detail(graph, node_id)
        if node is None:
            return jsonify({"error": "node not found"}), 404
        return jsonify(node)

    @app.get("/api/graphs/<graph_name>/neighbors")
    def graph_neighbors(graph_name: str):
        graph = client.select_graph(graph_name)
        node_id = request.args.get("id")
        if not node_id:
            return jsonify({"error": "missing 'id' query parameter"}), 400
        result = queries.neighbors(graph, node_id)
        if result is None:
            return jsonify({"error": "node not found"}), 404
        return jsonify(result)

    @app.get("/api/graphs/<graph_name>/path")
    def graph_path(graph_name: str):
        graph = client.select_graph(graph_name)
        from_id = request.args.get("from")
        to_id = request.args.get("to")
        if not from_id or not to_id:
            return jsonify({"error": "missing 'from' or 'to' query parameter"}), 400
        max_depth = request.args.get("max_depth", type=int) or queries.DEFAULT_PATH_DEPTH
        # any truthy value except an explicit "false"/"0" means directed
        directed = (request.args.get("directed", "true").lower() not in ("false", "0", "no"))
        try:
            result = queries.shortest_path(graph, from_id, to_id, max_depth, directed)
        except ValueError as e:  # max_depth out of range
            return jsonify({"error": str(e)}), 400
        except Exception as e:  # noqa: BLE001 -- e.g. FalkorDB query timeout: a clear message, not a 500
            msg = str(e)
            if "timed out" in msg.lower():
                msg = ("path search timed out — try enabling 'Follow edge direction', "
                       "fewer max hops, or a graph with fewer dense hubs")
            return jsonify({"error": msg}), 400
        if result is None:
            return jsonify({"error": "one or both endpoints not found"}), 404
        return jsonify(result)

    @app.get("/api/graphs/<graph_name>/all")
    def graph_all(graph_name: str):
        graph = client.select_graph(graph_name)
        limit = request.args.get("limit", type=int) or queries.DEFAULT_ALL_LIMIT
        return jsonify(queries.all_nodes_and_edges(graph, limit))

    @app.post("/api/graphs/<graph_name>/communities")
    def graph_communities(graph_name: str):
        # Read-only: label-propagation community detection over the whole graph,
        # result scoped to the caller's on-canvas node ids (see detect_communities).
        graph = client.select_graph(graph_name)
        body = request.get_json(silent=True) or {}
        ids = body.get("ids") or None
        try:
            return jsonify(queries.detect_communities(graph, ids))
        except Exception as e:  # noqa: BLE001 -- algo unsupported / query error -> 400, not 500
            return jsonify({"error": str(e)}), 400

    @app.post("/api/graphs/<graph_name>/community-names")
    def graph_community_names(graph_name: str):
        # Optional AI enrichment: name each community from its representative member
        # symbols. Pure LLM over the samples the caller sends (no graph access), so
        # it degrades to a 503 -- not a 500 -- when the anthropic SDK or an API
        # credential (ANTHROPIC_API_KEY) is absent; the viewer then keeps its
        # heuristic names. See viewer/community_names.py.
        body = request.get_json(silent=True) or {}
        communities = body.get("communities") or []
        if not communities:
            return jsonify({"labels": []})
        from auryn.viewer.community_names import label_communities

        try:
            return jsonify({"labels": label_communities(communities)})
        except Exception as e:  # noqa: BLE001 -- missing SDK/credential or API error
            return jsonify({"error": f"AI naming unavailable: {e}"}), 503

    @app.post("/api/graphs/<graph_name>/query")
    def graph_query(graph_name: str):
        # Read-only: run_read_query goes through GRAPH.RO_QUERY, so a write clause
        # is refused by FalkorDB itself. Any query error (write attempt, bad
        # syntax, timeout) is the caller's problem -> 400 with the DB's message,
        # never an unhandled 500.
        if graph_name not in client.list_graphs():
            return jsonify({"error": "graph not found"}), 404
        body = request.get_json(silent=True) or {}
        cypher = (body.get("cypher") or "").strip()
        if not cypher:
            return jsonify({"error": "missing 'cypher' in request body"}), 400
        graph = client.select_graph(graph_name)
        try:
            return jsonify(queries.run_read_query(graph, cypher))
        except Exception as e:  # noqa: BLE001 -- surface any query error as 400
            return jsonify({"error": str(e)}), 400

    @app.get("/api/graphs/<graph_name>/git-projects")
    def graph_git_projects(graph_name: str):
        if graph_name not in client.list_graphs():
            return jsonify({"error": "graph not found"}), 404
        graph = client.select_graph(graph_name)
        projects = queries.git_tracked_projects(graph)
        for project in projects:  # the card shows this instead of a ref picker that can only fail
            blocker = _sync_blocker(project)
            if blocker:
                project["sync_unavailable"] = blocker
        return jsonify(projects)

    @app.get("/api/graphs/<graph_name>/git-refs")
    def graph_git_refs(graph_name: str):
        # The only endpoint besides git-sync that does real I/O beyond a Cypher
        # query: it fetches from the remote so the branch/tag list is current,
        # not whatever this cache last had. Still never writes to the graph.
        from auryn.core.git_source import GitRepoCache, repos_cache_dir

        project_id = request.args.get("project_id")
        if not project_id:
            return jsonify({"error": "missing 'project_id' query parameter"}), 400
        if graph_name not in client.list_graphs():
            return jsonify({"error": "graph not found"}), 404

        graph = client.select_graph(graph_name)
        project = queries.node_detail(graph, project_id)
        if project is None or project["label"] != "Project":
            return jsonify({"error": "project not found"}), 404
        repo_uri = project["properties"].get("repo_uri")
        blocker = _sync_blocker(project)  # a missing repo_uri too
        if blocker:
            return jsonify({"error": blocker, "sync_unavailable": blocker}), 400  # as in git-projects
        try:
            refs = GitRepoCache(repo_uri, repos_cache_dir()).list_refs()
        except git.GitCommandError as e:
            return jsonify({"error": f"couldn't reach '{repo_uri}': {e}"}), 502
        return jsonify(refs)

    @app.post("/api/graphs/<graph_name>/git-sync")
    def graph_git_sync(graph_name: str):
        # The one write-capable endpoint in this file -- see module docstring.
        # Everything it needs (repo_uri/solution_name/dto_name_suffixes) comes from
        # the Project node's own properties, not a separate config store.
        body = request.get_json(silent=True) or {}
        project_id = body.get("project_id")
        if not project_id:
            return jsonify({"error": "missing 'project_id' in request body"}), 400
        ref = body.get("ref") or None

        if graph_name not in client.list_graphs():
            return jsonify({"error": "graph not found"}), 404

        graph = client.select_graph(graph_name)
        project = queries.node_detail(graph, project_id)
        if project is None or project["label"] != "Project":
            return jsonify({"error": "project not found"}), 404
        props = project["properties"]
        blocker = _sync_blocker(project)  # a missing repo_uri too
        if blocker:
            return jsonify({"error": blocker, "sync_unavailable": blocker}), 400  # as in git-projects
        # Only now: a refusal must not depend on the C# plugin being installed (auryn-core alone,
        # or with another language's plugin, serves the viewer too).
        from auryn.ingestors.csharp.git_sync import GitCSharpProjectConfig, sync

        config = GitCSharpProjectConfig(
            repo_uri=props["repo_uri"],
            project_name=props["name"],
            solution_name=props.get("solution_name"),
            target_framework=props.get("target_framework"),
            dto_name_suffixes=tuple(props.get("dto_name_suffixes") or GitCSharpProjectConfig.dto_name_suffixes),
        )
        try:
            stats = sync(GraphWriter(graph), config, ref)
        except ValueError as e:
            # e.g. a ref typed into the UI's "Other (enter commit sha)" field
            # that doesn't actually exist -- a user-input problem, not a server
            # error, so this must never surface as an unhandled 500.
            return jsonify({"error": str(e)}), 400
        except git.GitCommandError as e:
            return jsonify({"error": f"couldn't reach '{props['repo_uri']}': {e}"}), 502
        return jsonify(
            {
                "commit_sha": stats.commit_sha,
                "nodes_added": stats.nodes_added,
                "nodes_updated": stats.nodes_updated,
                "nodes_removed": stats.nodes_removed,
                "edges_added": stats.edges_added,
                "edges_updated": stats.edges_updated,
                "edges_removed": stats.edges_removed,
                "elapsed_seconds": stats.elapsed_seconds,
            }
        )

    return app
