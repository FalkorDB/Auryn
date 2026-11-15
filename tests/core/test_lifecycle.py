"""forget/list (ADR 0003): drop a dedicated graph, project-scoped delete in a shared graph
(prefix-collision safe), and DETACH cleanup of inbound cross-repo edges."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from auryn.core import lifecycle
from auryn.core.graph_client import GraphWriter
from auryn.core.ingest import ingest_directory


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


def _ingest(client, graph_name: str, path: Path, project: str) -> None:
    ingest_directory(GraphWriter(client.select_graph(graph_name)), str(path),
                     project_name=project, deep="off", auto_install=False)


def _count(g, cypher: str, params=None) -> int:
    return g.ro_query(cypher, params or {}).result_set[0][0]


def test_forget_dedicated_graph_is_dropped(falkordb_connection, tmp_path):
    client = falkordb_connection
    gname = f"ded_{uuid4().hex}"
    _write(tmp_path / "a", {"a.py": "def f():\n    return 1\n"})
    _ingest(client, gname, tmp_path / "a", "a")

    infos = {gi.graph_name: gi for gi in lifecycle.list_ingested(client)}
    assert gname in infos and any(p.name == "a" for p in infos[gname].projects)

    plan = lifecycle.plan_forget(client, gname)
    assert plan.mode == "drop_graph"
    lifecycle.execute_forget(client, plan)
    assert gname not in client.list_graphs()


def test_forget_project_in_shared_graph_is_prefix_safe(falkordb_connection, tmp_path):
    client = falkordb_connection
    gname = f"shared_{uuid4().hex}"
    _write(tmp_path / "acme", {"m.py": "def f():\n    return 1\n"})
    _write(tmp_path / "acmeapi", {"m.py": "def g():\n    return 2\n"})
    _ingest(client, gname, tmp_path / "acme", "acme")
    _ingest(client, gname, tmp_path / "acmeapi", "acme-api")  # "acme" is a string prefix of "acme-api"
    g = client.select_graph(gname)
    try:
        # forgetting the whole graph is ambiguous (two repos) -> must name the project
        with pytest.raises(lifecycle.ForgetError):
            lifecycle.plan_forget(client, gname)

        plan = lifecycle.plan_forget(client, gname, "acme")
        assert plan.mode == "drop_project" and plan.nodes > 0
        lifecycle.execute_forget(client, plan)

        names = {r[0] for r in g.ro_query("MATCH (p:Project) RETURN p.name").result_set}
        assert names == {"acme-api"}, "forget acme must leave acme-api (prefix-collision safe)"
        assert _count(g, "MATCH (n) WHERE n.id STARTS WITH 'python://acme/' RETURN count(n)") == 0
        assert _count(g, "MATCH (n) WHERE n.id STARTS WITH 'python://acme-api/' RETURN count(n)") > 0
    finally:
        g.delete()


def test_forget_demotes_inbound_cross_repo_edges(falkordb_connection, tmp_path):
    # ADR 0004: forgetting the target repo does NOT orphan the caller's edge -- it demotes the
    # target to a placeholder so the caller keeps an (now unresolved) reference.
    client = falkordb_connection
    gname = f"xrepo_{uuid4().hex}"
    _write(tmp_path / "aa", {"m.py": "def af():\n    return 1\n"})
    _write(tmp_path / "bb", {"n.py": "def bf():\n    return 2\n"})
    _ingest(client, gname, tmp_path / "aa", "aa")
    _ingest(client, gname, tmp_path / "bb", "bb")
    g = client.select_graph(gname)
    try:
        aid = g.ro_query("MATCH (f:Function) WHERE f.id STARTS WITH 'python://aa/' RETURN f.id LIMIT 1").result_set[0][0]
        bid = g.ro_query("MATCH (f:Function) WHERE f.id STARTS WITH 'python://bb/' RETURN f.id LIMIT 1").result_set[0][0]
        blink = g.ro_query("MATCH (f:Function {id:$b}) RETURN f.linkage_key", {"b": bid}).result_set[0][0]
        # a cross-repo call edge aa.af -> bb.bf (what real cross-repo resolution would create)
        g.query("MATCH (a:Function {id:$a}), (b:Function {id:$b}) MERGE (a)-[:CALLS]->(b)", {"a": aid, "b": bid})

        lifecycle.execute_forget(client, lifecycle.plan_forget(client, gname, "bb"))

        assert _count(g, "MATCH (f:Function {id:$a}) RETURN count(f)", {"a": aid}) == 1   # caller repo survives
        assert _count(g, "MATCH (f:Function {id:$b}) RETURN count(f)", {"b": bid}) == 0   # target repo gone
        # the caller keeps its call -- demoted onto a placeholder at the target's linkage
        rows = g.ro_query(
            "MATCH (:Function {id:$a})-[e:CALLS]->(t) RETURN t.id, t.resolved, e.resolution", {"a": aid}
        ).result_set
        assert len(rows) == 1
        tid, resolved, resn = rows[0]
        assert tid == blink and resolved is False and resn == "external"
    finally:
        g.delete()


# --- wire_trigger: the shared post-ingest step (CLI == MCP), honouring the install-time choice ---


def _git_repo(root: Path):
    import git

    root.mkdir(parents=True, exist_ok=True)
    git.Repo.init(str(root), initial_branch="main")
    return root


def test_wire_trigger_commit_installs_git_hook(monkeypatch, tmp_path):
    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "home"))
    from auryn.core import hooks
    from auryn.core.app_config import get_repo, set_resync_trigger

    repo = _git_repo(tmp_path / "r")
    set_resync_trigger("commit")

    status = lifecycle.wire_trigger(str(repo), "g", "proj")

    assert status == {"registered": True, "trigger": "commit", "hooks_installed": True}
    assert hooks.is_installed(str(repo))
    assert get_repo(str(repo)).graph_name == "g"


def test_wire_trigger_save_starts_watcher_and_no_hook(monkeypatch, tmp_path):
    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "home"))
    from auryn.core import hooks, watcher
    from auryn.core.app_config import set_resync_trigger

    repo = _git_repo(tmp_path / "r")
    set_resync_trigger("save")
    calls = []
    monkeypatch.setattr(watcher, "ensure_watcher", lambda: calls.append(True) or True)

    status = lifecycle.wire_trigger(str(repo), "g")

    assert status["trigger"] == "save" and status["watcher_running"] is True
    assert calls == [True]              # the folder watcher was set up...
    assert not hooks.is_installed(str(repo))  # ...and NO git hook for the save trigger


def test_wire_trigger_manual_registers_only(monkeypatch, tmp_path):
    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "home"))
    from auryn.core import hooks
    from auryn.core.app_config import get_repo, set_resync_trigger

    repo = _git_repo(tmp_path / "r")
    set_resync_trigger("manual")

    status = lifecycle.wire_trigger(str(repo), "g")

    assert status == {"registered": True, "trigger": "manual"}
    assert not hooks.is_installed(str(repo))
    assert get_repo(str(repo)) is not None   # still registered, just no auto-resync


def test_wire_trigger_no_local_checkout_is_skipped(monkeypatch, tmp_path):
    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "home"))
    status = lifecycle.wire_trigger(None, "g")
    assert status["registered"] is False and status["trigger"] is None
