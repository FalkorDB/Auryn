"""Unit tests for CUDA resolution (no DB). Proves the wrapper over the shared C++ resolver attaches
`execution_space` to the right Function/Method nodes and emits LAUNCHES edges only when both ends
resolve -- without recomputing any ids."""

from __future__ import annotations

from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.ingestors.cuda import resolution
from auryn.ingestors.cuda.file_parser import parse_source

_PROJ = "cuda://demo"


def _resolve(src: bytes, path="k.cu"):
    return list(resolution.resolve([parse_source(src, path)], _PROJ))


def _nodes(items, label):
    return [i for i in items if isinstance(i, GraphNode) and i.label == label]


def _launches(items):
    return [i for i in items if isinstance(i, GraphEdge) and i.rel_type == "LAUNCHES"]


def test_execution_space_on_nodes():
    items = _resolve(
        b"__global__ void k() {}\n"
        b"__device__ int d() { return 0; }\n"
        b"int plain() { return 0; }\n"
    )
    by_name = {n.properties["name"]: n.properties.get("execution_space") for n in _nodes(items, "Function")}
    assert by_name.get("k") == "global"
    assert by_name.get("d") == "device"
    # a plain host function carries execution_space=None (written explicitly so a `+=` merge clears a
    # stale value on resync; a null `+=` removes the property in the graph, so it reads as IS NULL)
    assert by_name.get("plain") is None


def test_launches_edge_resolves_to_kernel():
    items = _resolve(
        b"__global__ void addKernel(int *c) {}\n"
        b"void run(int n) { addKernel<<<1, n>>>(nullptr); }\n"
    )
    edges = _launches(items)
    assert len(edges) == 1
    e = edges[0]
    assert e.from_label == "Function" and e.to_label == "Function"
    # caller is run(), callee is the addKernel node (both cuda:// rooted)
    assert "run" in e.from_key["id"] and "addkernel" in e.to_key["id"]


def test_launch_of_unknown_kernel_emits_no_edge():
    # the launched kernel is not defined in this graph -> no LAUNCHES (the CALLS edge still captures it)
    items = _resolve(b"void run() { externalKernel<<<1,1>>>(nullptr); }\n")
    assert _launches(items) == []


def test_launch_target_must_be_global():
    # a same-named __device__ (not __global__) function is NOT a launch target
    items = _resolve(
        b"__device__ int notAKernel() { return 0; }\n"
        b"void run() { notAKernel<<<1,1>>>(); }\n"
    )
    assert _launches(items) == []


def test_duplicate_launches_dedup_to_one_edge():
    items = _resolve(
        b"__global__ void k() {}\n"
        b"void run() { k<<<1,1>>>(); k<<<2,2>>>(); }\n"
    )
    assert len(_launches(items)) == 1


def test_launch_resolves_to_scope_correct_kernel():
    # two kernels named `k` in different namespaces; a launcher in namespace b must LAUNCH b::k, not
    # a::k -- the target is taken from the C++ resolver's scope-aware CALLS edge (PR #31 review)
    items = _resolve(
        b"namespace a { __global__ void k() {} }\n"
        b"namespace b {\n"
        b"  __global__ void k() {}\n"
        b"  void run() { k<<<1,1>>>(); }\n"
        b"}\n"
    )
    edges = _launches(items)
    assert len(edges) == 1
    # b::k is the launch target (its id carries the `b` namespace, not `a`)
    assert "/namespace/b." in edges[0].to_key["id"]


def test_ambiguous_templated_launch_fallback_omits_edge():
    # two same-named TEMPLATED kernels in different namespaces: cpp drops the templated callee (no
    # CALLS to mirror), so the simple-name fallback runs -- but the name is ambiguous, so NO LAUNCHES
    # edge is emitted (no edge is better than a wrong one). PR #31 review.
    items = _resolve(
        b"namespace a { template<typename T> __global__ void k(T* x) {} }\n"
        b"namespace b { template<typename T> __global__ void k(T* x) {}\n"
        b"  void run(float* p) { k<float><<<1,1>>>(p); } }\n"
    )
    assert _launches(items) == []


def test_single_templated_launch_fallback_still_resolves():
    # a single templated kernel (unambiguous name) still LAUNCHES via the fallback
    items = _resolve(
        b"template<typename T> __global__ void reduce(T* x) {}\n"
        b"void run(float* p) { reduce<float><<<16,256>>>(p); }\n"
    )
    assert len(_launches(items)) == 1


def test_execution_space_always_written_for_stale_clearing():
    # every callable node carries execution_space (the value or None) so a `+=` merge clears a stale
    # value when a kernel loses its qualifier on resync (PR #31 review)
    items = _resolve(b"__global__ void k() {}\nint plain() { return 0; }\n")
    for n in _nodes(items, "Function"):
        assert "execution_space" in n.properties  # present on ALL callables, not only kernels
    by_name = {n.properties["name"]: n.properties["execution_space"] for n in _nodes(items, "Function")}
    assert by_name["k"] == "global" and by_name["plain"] is None


def test_launch_from_method_carries_method_from_label():
    items = _resolve(
        b"__global__ void k() {}\n"
        b"struct Grid { void go() { k<<<1,1>>>(); } };\n"
    )
    edges = _launches(items)
    assert len(edges) == 1 and edges[0].from_label == "Method"
