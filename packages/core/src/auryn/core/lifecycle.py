"""List and forget (delete) ingested repos -- the counter to ingest (ADR 0003).

Works entirely off the FalkorDB backend: an Auryn graph is one that carries a :Project
node (`<lang>://<name>`). A repo maps to a graph deterministically (core/project.py); the
default is one graph per repo, but several repos can share a graph (--graph-name). So `forget`
has two shapes:
  * dedicated graph (the repo is the only project in it): GRAPH.DELETE the whole graph;
  * shared graph: delete just that project's subgraph, prefix-scoped WITH THE TRAILING
    SEPARATOR so `acme` never takes `acme-api` with it, per-label so it uses the id range
    index, and via DETACH DELETE so inbound cross-repo edges are removed too.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ProjectInfo:
    name: str
    languages: list[str]
    commit_shas: dict[str, str]  # lang -> short sha


@dataclass
class GraphInfo:
    graph_name: str
    projects: list[ProjectInfo]
    nodes: int
    edges: int


@dataclass
class ForgetPlan:
    graph_name: str
    mode: str  # "drop_graph" | "drop_project"
    project_name: str | None
    languages: list[str]
    nodes: int  # nodes that will be removed


class ForgetError(Exception):
    """A forget that can't proceed (unknown graph/project, or an ambiguous shared graph)."""


def _projects_in(graph) -> dict[str, dict]:
    """Project nodes grouped by name -> {langs:set, shas:dict}; {} if not an Auryn graph."""
    try:
        rows = graph.ro_query("MATCH (p:Project) RETURN p.id, p.name, p.commit_sha").result_set
    except Exception:
        return {}
    by_name: dict[str, dict] = {}
    for pid, pname, sha in rows:
        lang = pid.split("://", 1)[0]
        info = by_name.setdefault(pname, {"langs": set(), "shas": {}})
        info["langs"].add(lang)
        info["shas"][lang] = (sha or "")[:12]
    return by_name


def list_ingested(client) -> list[GraphInfo]:
    """Every Auryn graph on the backend (those carrying a :Project node), with its
    project(s), languages, shas, and node/edge counts. Graphs that aren't Auryn's are
    skipped."""
    out: list[GraphInfo] = []
    for name in client.list_graphs():
        g = client.select_graph(name)
        by_name = _projects_in(g)
        if not by_name:
            continue
        nodes = g.ro_query("MATCH (n) RETURN count(n)").result_set[0][0]
        edges = g.ro_query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0]
        projects = [ProjectInfo(nm, sorted(v["langs"]), v["shas"]) for nm, v in sorted(by_name.items())]
        out.append(GraphInfo(name, projects, nodes, edges))
    return out


def plan_forget(client, graph_name: str, project_name: str | None = None) -> ForgetPlan:
    """Decide what deleting a repo entails, without mutating anything (for confirmation /
    --dry-run). Raises ForgetError for an unknown graph/project or an ambiguous shared graph
    (multiple repos, no --project)."""
    if graph_name not in client.list_graphs():
        raise ForgetError(f"no such graph: {graph_name!r}")
    g = client.select_graph(graph_name)
    by_name = _projects_in(g)
    if not by_name:
        raise ForgetError(f"{graph_name!r} is not an Auryn graph (no Project node)")

    if project_name is None:
        if len(by_name) > 1:
            raise ForgetError(
                f"graph {graph_name!r} holds multiple repos ({', '.join(sorted(by_name))}); "
                "pass --project <name> to forget just one"
            )
        project_name = next(iter(by_name))
    elif project_name not in by_name:
        raise ForgetError(f"no project {project_name!r} in graph {graph_name!r} "
                          f"(have: {', '.join(sorted(by_name))})")

    langs = sorted(by_name[project_name]["langs"])
    if len(by_name) == 1:  # this repo IS the graph -> drop it wholesale
        total = g.ro_query("MATCH (n) RETURN count(n)").result_set[0][0]
        return ForgetPlan(graph_name, "drop_graph", project_name, langs, total)
    return ForgetPlan(graph_name, "drop_project", project_name, langs, _count_project_nodes(g, project_name, langs))


def execute_forget(client, plan: ForgetPlan) -> None:
    g = client.select_graph(plan.graph_name)
    if plan.mode == "drop_graph":
        g.delete()
        return
    # Preserve cross-repo references INTO this project before deleting it: demote each of its
    # still-referenced externally-visible nodes back to a placeholder (ADR 0004), so a consumer
    # repo keeps an unresolved edge rather than losing it to DETACH DELETE.
    from auryn.core.linkage import demote_cross_repo

    demote_cross_repo(g, [f"{lang}://{plan.project_name}" for lang in plan.languages])

    labels = _labels(g)
    for lang in plan.languages:
        prefix = f"{lang}://{plan.project_name}"
        lo, hi = prefix + "/", prefix + "/" + chr(0xFFFF)  # trailing "/" -> acme never takes acme-api
        for label in labels:
            # Two index-backed deletes per label -- the subtree by id range, the Project root by
            # exact id. NOT a single `id = exact OR (id >= lo AND id < hi)`: that OR form makes
            # FalkorDB drop the range's upper bound and delete sibling projects sorting after this
            # one (a shared-graph data-loss bug). DETACH also clears inbound cross-repo edges left
            # after demote_cross_repo has preserved the ones worth keeping.
            g.query(
                f"MATCH (n:{label}) WHERE n.id >= $lo AND n.id < $hi DETACH DELETE n",
                {"lo": lo, "hi": hi},
            )
            g.query(f"MATCH (n:{label} {{id: $exact}}) DETACH DELETE n", {"exact": prefix})


def _labels(g) -> list[str]:
    # Exclude the system-wide labels (Entity/Embeddable) that ride on every/most nodes -- iterating
    # them here would make forget() re-delete the same nodes and _count_project_nodes() double-count
    # them (every node is also an :Entity). Callers want the dedicated per-kind labels only.
    from auryn.core.graph_client import _SHARED_LABELS

    return [r[0] for r in g.ro_query("CALL db.labels()").result_set if r[0] not in _SHARED_LABELS]


def _count_project_nodes(g, project_name: str, langs: list[str]) -> int:
    labels = _labels(g)
    total = 0
    for lang in langs:
        prefix = f"{lang}://{project_name}"
        lo, hi = prefix + "/", prefix + "/" + chr(0xFFFF)
        for label in labels:
            total += g.ro_query(
                f"MATCH (n:{label}) WHERE n.id >= $lo AND n.id < $hi RETURN count(n)",
                {"lo": lo, "hi": hi},
            ).result_set[0][0]
            total += g.ro_query(
                f"MATCH (n:{label} {{id: $exact}}) RETURN count(n)", {"exact": prefix}
            ).result_set[0][0]
    return total


def wire_trigger(
    local_dir, graph_name: str, project_name: str | None = None, trigger: str | None = None
) -> dict:
    """Register a repo and set up its resync trigger per the user's install-time choice (ADR 0003).

    This is the SHARED post-ingest step for BOTH the CLI (`ingest`) and the MCP index tools, so
    agent-driven and human-driven ingestion have identical effects -- an MCP ingest can never
    silently skip what a CLI ingest does. The repo is registered, then the install-wide
    ``[resync].trigger`` the user picked at install time is honoured (unless `trigger` overrides
    it): ``commit`` installs the git hooks, ``save`` starts the folder watcher, ``manual`` sets
    up nothing. `local_dir` is None for a remote-URI ingest with no local checkout, in which case
    registry/trigger are skipped. Returns a status dict describing what was wired."""
    import sys

    from auryn.core import hooks
    from auryn.core.app_config import RepoRegistration, load_config, register_repo

    if local_dir is None:
        return {
            "registered": False,
            "trigger": None,
            "note": "no local checkout; resync trigger not wired -- clone locally, then `auryn sync`",
        }

    local_dir = str(local_dir)
    default_trigger = load_config().resync_trigger  # the install-time choice
    trigger = trigger or default_trigger
    # Persist only a per-repo override, never the install default (matches the CLI / ADR 0003).
    register_repo(
        RepoRegistration(
            local_dir=local_dir,
            graph_name=graph_name,
            project_name=project_name,
            trigger=None if trigger == default_trigger else trigger,
        )
    )
    status: dict = {"registered": True, "trigger": trigger}

    if trigger == "commit":
        if hooks.is_git_repo(local_dir):
            wired = hooks.install(local_dir, [sys.executable, "-m", "auryn", "sync", local_dir])
            status["hooks_installed"] = bool(wired)
            if not wired:
                status["note"] = ("core.hooksPath points outside this repo's git dir (tracked or shared "
                                  "hooks); hook skipped -- use `--trigger save` or `auryn sync`")
        else:
            status["hooks_installed"] = False
            status["note"] = "commit trigger needs a git repo; hook skipped -- use `auryn sync`"
    elif trigger == "save":
        from auryn.core import watcher

        status["watcher_running"] = bool(watcher.ensure_watcher())
    return status
