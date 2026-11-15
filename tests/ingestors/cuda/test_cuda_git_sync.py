"""Integration tests for CUDA ingestion wiring, against the real `graph` fixture. Proves the CUDA
plugin owns `.cu`/`.cuh` in the multi-language endpoint, that the shared C++ engine models the base
structure end-to-end over the tree-sitter-cuda grammar, and that the two CUDA-only signals land in
the graph: `execution_space` on kernels/device/host functions and LAUNCHES edges for `<<<...>>>`
launches. Needs FalkorDB (the conftest fixture)."""

from __future__ import annotations

from auryn.core import ingest
from auryn.core.graph_client import GraphWriter

_SAMPLE = {
    "kernels.cuh": (
        "#pragma once\n"
        "namespace demo {\n"
        "__global__ void addKernel(int *c, const int *a, const int *b);\n"
        "__device__ int scale(int x);\n"
        "}\n"
    ),
    "kernels.cu": (
        '#include "kernels.cuh"\n'
        "namespace demo {\n"
        "__device__ int scale(int x) { return x * 2; }\n"
        "__global__ void addKernel(int *c, const int *a, const int *b) {\n"
        "    int i = 0;\n"
        "    c[i] = scale(a[i]) + b[i];\n"
        "}\n"
        "}\n"
    ),
    "main.cu": (
        '#include "kernels.cuh"\n'
        "namespace demo {\n"
        "__host__ void launch(int *c, const int *a, const int *b, int n) {\n"
        "    addKernel<<<1, n>>>(c, a, b);\n"
        "    cudaDeviceSynchronize();\n"
        "}\n"
        "}\n"
    ),
}


def _write_sample(tmp_path):
    for name, text in _SAMPLE.items():
        (tmp_path / name).write_text(text)
    return tmp_path


def _ingest(graph, tmp_path, project_name="demo"):
    _write_sample(tmp_path)
    report = ingest.ingest_directory(GraphWriter(graph), str(tmp_path), project_name=project_name)

    def q(cypher):
        return graph.query(cypher).result_set

    return report, q


def test_ingest_directory_owns_cuda(graph, tmp_path):
    report, _ = _ingest(graph, tmp_path)
    ingested = {r.language: r for r in report.languages if r.result is not None}
    assert "cuda" in ingested
    assert ingested["cuda"].files_detected == 3  # kernels.cuh + kernels.cu + main.cu
    assert not any(u["language"] == "cuda" for u in report.unsupported_detected)


def test_execution_space_recorded(graph, tmp_path):
    _, q = _ingest(graph, tmp_path)
    # the kernel is marked __global__
    assert q("MATCH (f:Function {name:'addKernel'}) RETURN f.execution_space") == [["global"]]
    # the device helper is __device__
    assert q("MATCH (f:Function {name:'scale'}) RETURN f.execution_space") == [["device"]]
    # the host launcher is __host__
    assert q("MATCH (f:Function {name:'launch'}) RETURN f.execution_space") == [["host"]]
    # "find every kernel" is a single predicate over the indexed property
    assert q("MATCH (f:Function {execution_space:'global'}) RETURN f.name") == [["addKernel"]]


def test_launches_edge_in_graph(graph, tmp_path):
    _, q = _ingest(graph, tmp_path)
    # launch() -LAUNCHES-> addKernel kernel
    assert q(
        "MATCH (:Function {name:'launch'})-[:LAUNCHES]->(k:Function {name:'addKernel'}) RETURN 1"
    ) == [[1]]
    # the launch is ALSO an ordinary CALLS edge (LAUNCHES is a typed overlay, not a replacement)
    assert q(
        "MATCH (:Function {name:'launch'})-[:CALLS]->(:Function {name:'addKernel'}) RETURN 1"
    ) == [[1]]


def test_base_cpp_structure_present(graph, tmp_path):
    _, q = _ingest(graph, tmp_path)
    # namespace tier + the device-side call scale() inside the kernel both resolve via the C++ engine
    assert q("MATCH (:Project)-[:HAS_NAMESPACE]->(n:Namespace {name:'demo'}) RETURN 1") == [[1]]
    assert q("MATCH (:Function {name:'addKernel'})-[:CALLS]->(:Function {name:'scale'}) RETURN 1") == [[1]]


def test_cuda_ids_namespaced(graph, tmp_path):
    _, q = _ingest(graph, tmp_path)
    # CUDA nodes are rooted at cuda:// (distinct from a c:// or cpp:// subgraph of the same name)
    assert q("MATCH (f:Function {name:'addKernel'}) RETURN f.id")[0][0].startswith("cuda://demo")


def test_resync_clears_stale_execution_space(graph, tmp_path):
    """A kernel that loses its __global__ qualifier across a resync must not keep a stale
    execution_space -- execution_space is written explicitly (None when absent), and a null `+=`
    merge removes the property (PR #31 review)."""
    f = tmp_path / "k.cu"
    f.write_text("__global__ void maybe(int *c) { c[0] = 0; }\n")
    ingest.ingest_directory(GraphWriter(graph), str(tmp_path), project_name="flip")
    assert graph.query("MATCH (f:Function {name:'maybe'}) RETURN f.execution_space").result_set == [["global"]]

    # the function is no longer a kernel -- re-ingest (full recompute)
    f.write_text("void maybe(int *c) { c[0] = 0; }\n")
    ingest.ingest_directory(GraphWriter(graph), str(tmp_path), project_name="flip")
    assert graph.query("MATCH (f:Function {name:'maybe'}) RETURN f.execution_space").result_set == [[None]]


def test_reingest_is_noop(graph, tmp_path):
    """A second ingest of the unchanged tree is a full recompute whose delta is empty -- no churn."""
    _ingest(graph, tmp_path)
    before = graph.query("MATCH (n) RETURN count(n)").result_set[0][0]
    report = ingest.ingest_directory(GraphWriter(graph), str(tmp_path), project_name="demo")
    cuda = next(r for r in report.languages if r.language == "cuda")
    assert cuda.result.nodes_added == 0 and cuda.result.nodes_removed == 0
    assert cuda.result.edges_added == 0 and cuda.result.edges_removed == 0
    after = graph.query("MATCH (n) RETURN count(n)").result_set[0][0]
    assert before == after
