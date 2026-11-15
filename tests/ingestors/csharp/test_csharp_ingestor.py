"""Integration tests against a real FalkorDB graph (the `graph` fixture from
tests/conftest.py). Unlike PL/SQL, no external service is needed beyond that -- just
a small multi-file C# "project" written to tmp_path.

Covers the full shape end to end (a controller, a DTO, a service pair behind an
interface, an enum), idempotency, and that results are identical with workers=1 vs
workers=2+ -- proving the ProcessPoolExecutor parallelism doesn't change output, not
just that it doesn't crash.
"""

from __future__ import annotations

from pathlib import Path

from auryn.core.graph_client import GraphWriter
from auryn.ingestors.csharp.config import CSharpSourceConfig
from auryn.ingestors.csharp.ingestor import CSharpIngestor

_CONTROLLER_CS = """
using Microsoft.AspNetCore.Mvc;

namespace Acme.Widgets
{
    [ApiController]
    [Route("api/[controller]")]
    public class WidgetsController : ControllerBase
    {
        private readonly IWidgetService _service;

        public WidgetsController(IWidgetService service)
        {
            _service = service;
        }

        [HttpGet("{id}")]
        public WidgetDto GetById(int id)
        {
            return _service.GetById(id);
        }

        [HttpPost]
        public WidgetDto Create([FromBody] CreateWidgetRequest request)
        {
            return _service.Create(request);
        }
    }
}
"""

_SERVICE_CS = """
namespace Acme.Widgets
{
    public interface IWidgetService
    {
        WidgetDto GetById(int id);
        WidgetDto Create(CreateWidgetRequest request);
    }

    public class WidgetService : IWidgetService
    {
        public WidgetDto GetById(int id)
        {
            return new WidgetDto();
        }

        public WidgetDto Create(CreateWidgetRequest request)
        {
            return new WidgetDto();
        }
    }
}
"""

_MODELS_CS = """
namespace Acme.Widgets
{
    public class WidgetDto
    {
        public int Id { get; set; }
        public string Name { get; set; }
    }

    public class CreateWidgetRequest
    {
        public string Name { get; set; }
    }

    public enum WidgetStatus : byte
    {
        Active = 1,
        Retired = 2,
    }
}
"""


def _write_sample_project(root: Path) -> Path:
    (root / "Controllers").mkdir()
    (root / "Controllers" / "WidgetsController.cs").write_text(_CONTROLLER_CS)
    (root / "Services.cs").write_text(_SERVICE_CS)
    (root / "Models.cs").write_text(_MODELS_CS)
    # bin/obj noise -- must be excluded from discovery, like a real build output dir
    (root / "obj").mkdir()
    (root / "obj" / "Generated.cs").write_text("namespace Acme.Widgets { public class ShouldBeIgnored { } }")
    return root


def _run(tmp_path, graph, workers):
    config = CSharpSourceConfig(path=str(tmp_path), project_name="Widgets", workers=workers)
    return CSharpIngestor(config).run(GraphWriter(graph))


def test_csharp_ingestor_creates_expected_graph_shape(tmp_path, graph):
    _write_sample_project(tmp_path)
    _run(tmp_path, graph, workers=1)

    counts = {row[0]: row[1] for row in graph.query("MATCH (n) RETURN labels(n)[0], count(n)").result_set}
    assert counts["Project"] == 1
    assert counts["Namespace"] == 2  # Acme, Acme.Widgets
    assert counts["File"] == 3  # obj/Generated.cs must be excluded
    assert counts["Class"] == 2  # WidgetsController, WidgetService
    assert counts["Interface"] == 1
    assert counts["DTO"] == 2  # WidgetDto, CreateWidgetRequest
    assert counts["Enum"] == 1
    assert counts.get("EnumMember") == 2

    # obj/ was actually excluded, not just under-counted by coincidence.
    assert graph.query("MATCH (c:Class {name: 'ShouldBeIgnored'}) RETURN c").result_set == []

    implements = graph.query(
        "MATCH (c:Class {name: 'WidgetService'})-[:IMPLEMENTS]->(i:Interface) RETURN i.name"
    ).result_set
    assert implements == [["IWidgetService"]]

    endpoints = graph.query(
        """
        MATCH (:Class {name: 'WidgetsController'})-[:HAS_METHOD]->(m:Method)-[:EXPOSES_ENDPOINT]->(e:APIEndpoint)
        RETURN m.name, e.http_method, e.route
        ORDER BY m.name
        """
    ).result_set
    # Routes are normalized (lowercased, leading slash) so a caller reaching the same route keys to
    # this same node and merges instead of duplicating (ADR 0006 dedup).
    assert endpoints == [
        ["Create", "POST", "/api/widgets"],
        ["GetById", "GET", "/api/widgets/{id}"],
    ]

    accepts = graph.query(
        "MATCH (e:APIEndpoint {name: 'Create'})-[:ACCEPTS]->(d:DTO) RETURN d.name"
    ).result_set
    assert accepts == [["CreateWidgetRequest"]]

    returns = graph.query(
        "MATCH (e:APIEndpoint)-[:RETURNS]->(d:DTO {name: 'WidgetDto'}) RETURN e.name ORDER BY e.name"
    ).result_set
    assert returns == [["Create"], ["GetById"]]

    # The controller's own call to _service.GetById() must land on the interface's
    # method, not spuriously loop back onto itself (both are named "GetById").
    call_edges = graph.query(
        """
        MATCH (a:Method {name: 'GetById'})-[r:CALLS]->(b:Method {name: 'GetById'})
        MATCH (a)<-[:HAS_METHOD]-(fromType), (b)<-[:HAS_METHOD]-(toType)
        RETURN labels(fromType)[0], labels(toType)[0]
        """
    ).result_set
    assert call_edges == [["Class", "Interface"]]


def test_csharp_ingestor_is_idempotent(tmp_path, graph):
    _write_sample_project(tmp_path)
    _run(tmp_path, graph, workers=1)
    counts_after_first = (
        graph.query("MATCH (n) RETURN count(n)").result_set[0][0],
        graph.query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0],
    )

    _run(tmp_path, graph, workers=1)
    counts_after_second = (
        graph.query("MATCH (n) RETURN count(n)").result_set[0][0],
        graph.query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0],
    )

    assert counts_after_first == counts_after_second


def test_csharp_ingestor_parallel_workers_produce_the_same_graph_as_sequential(tmp_path, graph, falkordb_connection):
    from uuid import uuid4

    _write_sample_project(tmp_path)

    _run(tmp_path, graph, workers=1)
    sequential_shape = sorted(
        graph.query(
            "MATCH (n) RETURN labels(n)[0], n.name, count(*) ORDER BY labels(n)[0], n.name"
        ).result_set
    )

    parallel_graph = falkordb_connection.select_graph(f"ingestor_test_{uuid4().hex}")
    try:
        _run(tmp_path, parallel_graph, workers=2)
        parallel_shape = sorted(
            parallel_graph.query(
                "MATCH (n) RETURN labels(n)[0], n.name, count(*) ORDER BY labels(n)[0], n.name"
            ).result_set
        )
        assert sequential_shape == parallel_shape
    finally:
        parallel_graph.delete()
