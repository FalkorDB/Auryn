"""Integration test for git_sync.sync() against the real `graph` fixture and a
local git repo built with GitPython in tmp_path (no network needed).

Commit 2 deliberately reproduces the DTO-reclassification-cascade scenario from
the feature's design discussion: an endpoint added in one file flips a *different*,
untouched file's Class to a DTO. This is the concrete case that proves the "full
recompute + diff" mechanism (chosen instead of deriving edits from a file-level git
diff) actually does what it's for.
"""

from __future__ import annotations

import git
import pytest

from auryn.core.graph_client import GraphWriter
from auryn.core.graph_model import GraphNode
from auryn.ingestors.csharp.git_sync import GitCSharpProjectConfig, sync

_WIDGET_CS = """
namespace Acme
{
    public class Widget
    {
        public int Id { get; set; }
    }
}
"""

_CONTROLLER_CS = """
using Microsoft.AspNetCore.Mvc;

namespace Acme
{
    [ApiController]
    [Route("api/[controller]")]
    public class WidgetsController : ControllerBase
    {
        [HttpGet]
        public Widget GetOne()
        {
            return new Widget();
        }
    }
}
"""


@pytest.fixture
def origin_repo(tmp_path):
    origin_path = tmp_path / "origin"
    repo = git.Repo.init(str(origin_path), initial_branch="main")
    with repo.config_writer() as cw:
        cw.set_value("user", "email", "test@example.com")
        cw.set_value("user", "name", "Test")

    (origin_path / "src").mkdir()
    (origin_path / "src" / "Widget.cs").write_text(_WIDGET_CS)
    repo.index.add(["src/Widget.cs"])
    first_commit = repo.index.commit("plain Widget class").hexsha

    (origin_path / "src" / "WidgetsController.cs").write_text(_CONTROLLER_CS)
    repo.index.add(["src/WidgetsController.cs"])
    second_commit = repo.index.commit("add endpoint returning Widget").hexsha

    return origin_path, first_commit, second_commit


@pytest.fixture
def config(origin_repo):
    origin_path, _, _ = origin_repo
    return GitCSharpProjectConfig(repo_uri=str(origin_path), project_name="Widgets", workers=1)


def test_files_parsed_and_cached_report_cold_then_warm_correctly(origin_repo, graph, tmp_path, config):
    _, first_commit, second_commit = origin_repo
    writer = GraphWriter(graph)
    cache_root = tmp_path / "cache"

    cold = sync(writer, config, ref=first_commit, cache_root=cache_root, engine="treesitter")
    assert cold.files_parsed == 1  # Widget.cs, never seen before
    assert cold.files_cached == 0

    warm = sync(writer, config, ref=second_commit, cache_root=cache_root, engine="treesitter")
    assert warm.files_parsed == 1  # only the new WidgetsController.cs
    assert warm.files_cached == 1  # Widget.cs reused verbatim from the parse cache


def test_first_sync_ingests_the_full_graph_and_records_the_commit(origin_repo, graph, tmp_path, config):
    _, first_commit, _ = origin_repo
    writer = GraphWriter(graph)

    stats = sync(writer, config, ref=first_commit, cache_root=tmp_path / "cache", engine="treesitter")

    assert stats.commit_sha == first_commit
    assert graph.query("MATCH (c:Class {name: 'Widget'}) RETURN c").result_set != []
    assert graph.query("MATCH (d:DTO) RETURN d").result_set == []
    assert graph.query(
        "MATCH (p:Project {id: 'csharp://Widgets'}) RETURN p.commit_sha"
    ).result_set == [[first_commit]]


def test_sync_forward_flips_an_untouched_files_class_to_dto(origin_repo, graph, tmp_path, config):
    _, first_commit, second_commit = origin_repo
    writer = GraphWriter(graph)
    sync(writer, config, ref=first_commit, cache_root=tmp_path / "cache", engine="treesitter")

    stats = sync(writer, config, ref=second_commit, cache_root=tmp_path / "cache", engine="treesitter")

    assert stats.commit_sha == second_commit
    # Widget.cs itself was never touched by the second commit -- only
    # WidgetsController.cs was added -- yet Widget must now be a DTO.
    assert graph.query("MATCH (c:Class {name: 'Widget'}) RETURN c").result_set == []
    assert graph.query("MATCH (d:DTO {name: 'Widget'}) RETURN d").result_set != []
    assert graph.query("MATCH (c:Class {name: 'WidgetsController'}) RETURN c").result_set != []
    assert graph.query(
        "MATCH (:Method {name: 'GetOne'})-[:EXPOSES_ENDPOINT]->(e:APIEndpoint)-[:RETURNS]->(d:DTO {name: 'Widget'}) RETURN e"
    ).result_set != []


def test_sync_backward_reverts_the_dto_flip(origin_repo, graph, tmp_path, config):
    _, first_commit, second_commit = origin_repo
    writer = GraphWriter(graph)
    sync(writer, config, ref=first_commit, cache_root=tmp_path / "cache", engine="treesitter")
    sync(writer, config, ref=second_commit, cache_root=tmp_path / "cache", engine="treesitter")

    stats = sync(writer, config, ref=first_commit, cache_root=tmp_path / "cache", engine="treesitter")

    assert stats.commit_sha == first_commit
    assert graph.query("MATCH (d:DTO) RETURN d").result_set == []
    assert graph.query("MATCH (c:Class {name: 'Widget'}) RETURN c").result_set != []
    assert graph.query("MATCH (c:Class {name: 'WidgetsController'}) RETURN c").result_set == []
    assert graph.query("MATCH (m:Method) RETURN m").result_set == []  # the controller's action is gone too
    # Every node the C# PROJECT owns reverts. The route-keyed APIEndpoint is a shared, cross-repo node
    # (deliberately outside any single project's id-prefix, so callers in other repos converge on it),
    # which is why this bare project sync can't reclaim it -- the http-endpoints stitch GCs it as a
    # global orphan on the next real ingest (see stitch._gc_orphan_endpoints). It's a stale endpoint,
    # never a duplicate.
    non_shared = graph.query("MATCH (n) WHERE NOT n:APIEndpoint RETURN count(n)").result_set[0][0]
    assert non_shared == 5  # Project/Namespace/File/Class/Property


def test_resyncing_the_same_ref_is_a_zero_delta_noop(origin_repo, graph, tmp_path, config):
    _, first_commit, _ = origin_repo
    writer = GraphWriter(graph)
    sync(writer, config, ref=first_commit, cache_root=tmp_path / "cache", engine="treesitter")

    stats = sync(writer, config, ref=first_commit, cache_root=tmp_path / "cache", engine="treesitter")

    assert stats == type(stats)(commit_sha=first_commit, elapsed_seconds=stats.elapsed_seconds)


def test_parallel_parsing_produces_the_same_graph_as_sequential(origin_repo, graph, falkordb_connection, tmp_path):
    """workers=2 forces both .cs files to parse concurrently via ProcessPoolExecutor
    (a fresh cache dir per run, so neither hits the blob-sha parse cache) --
    proves the parallelism doesn't change the result, same reasoning as the plain
    CSharpIngestor's own workers=1 vs workers=2+ parity test."""
    from uuid import uuid4

    origin_path, _, second_commit = origin_repo
    sequential_config = GitCSharpProjectConfig(repo_uri=str(origin_path), project_name="Widgets", workers=1)
    sync(GraphWriter(graph), sequential_config, ref=second_commit, cache_root=tmp_path / "cache_sequential", engine="treesitter")
    sequential_shape = sorted(
        graph.query("MATCH (n) RETURN labels(n)[0], n.name, count(*) ORDER BY 1, 2").result_set
    )

    parallel_graph = falkordb_connection.select_graph(f"ingestor_test_{uuid4().hex}")
    try:
        parallel_config = GitCSharpProjectConfig(repo_uri=str(origin_path), project_name="Widgets", workers=2)
        sync(GraphWriter(parallel_graph), parallel_config, ref=second_commit, cache_root=tmp_path / "cache_parallel", engine="treesitter")
        parallel_shape = sorted(
            parallel_graph.query("MATCH (n) RETURN labels(n)[0], n.name, count(*) ORDER BY 1, 2").result_set
        )
        assert sequential_shape == parallel_shape
    finally:
        parallel_graph.delete()


def test_cross_repo_call_heals_to_the_real_method_in_a_shared_graph(graph, tmp_path):
    """ADR 0004: two C# repos ingested into ONE graph. A call in the consumer to a method the
    producer defines starts as a placeholder (no definer yet) and heals onto the real method once
    the producer is ingested -- the reconcile step wired into git_sync._apply, exercised end-to-end
    with the fast tree-sitter engine."""
    def _repo(name, filename, content):
        path = tmp_path / name
        path.mkdir()
        repo = git.Repo.init(str(path), initial_branch="main")
        with repo.config_writer() as cw:
            cw.set_value("user", "email", "t@e.com")
            cw.set_value("user", "name", "T")
        (path / filename).write_text(content)
        repo.index.add([filename])
        return str(path), repo.index.commit("init").hexsha

    consumer_src = (
        "namespace Shop {\n"
        "    public class Checkout {\n"
        "        private Acme.Billing _billing;\n"
        "        public void Place() { _billing.Charge(); }\n"
        "    }\n"
        "}\n"
    )
    producer_src = (
        "namespace Acme {\n"
        "    public class Billing {\n"
        "        public void Charge() { }\n"
        "    }\n"
        "}\n"
    )
    consumer_path, consumer_sha = _repo("web", "Checkout.cs", consumer_src)
    producer_path, producer_sha = _repo("lib", "Billing.cs", producer_src)
    writer = GraphWriter(graph)

    # Consumer first: the cross-repo call has no definer yet -> a placeholder keyed by linkage.
    sync(writer, GitCSharpProjectConfig(repo_uri=consumer_path, project_name="Web", workers=1),
         ref=consumer_sha, cache_root=tmp_path / "cache", engine="treesitter")
    assert graph.query(
        "MATCH (:Method {name:'Place'})-[:CALLS]->(m:Method {resolved:false}) RETURN m.id"
    ).result_set == [["extern://csharp/Acme.Billing.Charge"]]

    # Producer next: reconcile folds the placeholder into the real Acme.Billing.Charge.
    sync(writer, GitCSharpProjectConfig(repo_uri=producer_path, project_name="Lib", workers=1),
         ref=producer_sha, cache_root=tmp_path / "cache", engine="treesitter")

    # The call now reaches the real, project-scoped method (no `resolved:false`), and no
    # placeholder Method survives anywhere in the graph.
    assert graph.query(
        "MATCH (:Method {name:'Place'})-[:CALLS]->(m:Method {name:'Charge'}) RETURN m.id, m.resolved"
    ).result_set == [["csharp://Lib/ns/acme.class.billing.method.charge()", None]]
    assert graph.query("MATCH (m:Method {resolved:false}) RETURN count(m)").result_set == [[0]]


def test_sync_never_touches_another_projects_nodes_in_the_same_graph(origin_repo, graph, tmp_path, config):
    _, first_commit, second_commit = origin_repo
    writer = GraphWriter(graph)
    writer.merge_node(GraphNode("Table", {"id": "oracle://other/employees"}, {"name": "employees"}))

    sync(writer, config, ref=first_commit, cache_root=tmp_path / "cache", engine="treesitter")
    sync(writer, config, ref=second_commit, cache_root=tmp_path / "cache", engine="treesitter")

    assert graph.query("MATCH (t:Table {id: 'oracle://other/employees'}) RETURN t.name").result_set == [
        ["employees"]
    ]


class TestTwoPhaseDepth:
    """C# now shares Python's fast/deep contract: `--deep off` runs tree-sitter (records
    depth='fast'); the deep pass layers Roslyn (depth='deep'); and a fast re-run never
    downgrades a deep graph, thanks to the depth-aware short-circuit in sync_at()."""

    @staticmethod
    def _depth(graph):
        rows = graph.query("MATCH (p:Project {id:'csharp://Widgets'}) RETURN p.depth").result_set
        return rows[0][0] if rows else None

    def test_treesitter_pass_records_fast_depth(self, origin_repo, graph, tmp_path, config):
        _, first_commit, _ = origin_repo
        sync(GraphWriter(graph), config, ref=first_commit, cache_root=tmp_path / "c", engine="treesitter")
        assert self._depth(graph) == "fast"

    def test_fast_rerun_does_not_downgrade_a_deep_graph(self, origin_repo, graph, tmp_path, config):
        _, first_commit, _ = origin_repo
        writer = GraphWriter(graph)
        sync(writer, config, ref=first_commit, cache_root=tmp_path / "c", engine="treesitter")
        # Simulate a completed deep (Roslyn) pass without needing the .NET SDK in this environment.
        graph.query("MATCH (p:Project {id:'csharp://Widgets'}) SET p.depth='deep'")
        stats = sync(writer, config, ref=first_commit, cache_root=tmp_path / "c", engine="treesitter")
        # deep >= fast, so the fast re-run short-circuits to a no-op and leaves the deep graph intact
        assert stats == type(stats)(commit_sha=first_commit, elapsed_seconds=stats.elapsed_seconds)
        assert self._depth(graph) == "deep"
