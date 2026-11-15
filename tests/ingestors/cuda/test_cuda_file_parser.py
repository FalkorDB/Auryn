"""Unit tests for the CUDA file parser (no DB). CUDA is a strict superset of C++, so the base C++
structure comes from the shared engine; these tests focus on the CUDA-only side-cars (execution
space per function/method, and kernel-launch sites) plus a sanity check that the C++ structure still
flows through."""

from __future__ import annotations

from auryn.ingestors.cuda.file_parser import parse_source


def test_execution_space_qualifiers():
    src = (
        b"__global__ void k(int *c) { c[0] = 1; }\n"
        b"__device__ int d(int x) { return x; }\n"
        b"__host__ void h() {}\n"
        b"__host__ __device__ int hd() { return 0; }\n"
        b"int plain() { return 0; }\n"
    )
    rf = parse_source(src, "k.cu")
    spaces = {name: space for (name, _line), space in rf.exec_spaces.items()}
    assert spaces == {"k": "global", "d": "device", "h": "host", "hd": "host_device"}
    # a plain (unqualified) host function gets NO execution_space entry
    assert "plain" not in spaces


def test_kernel_launch_sites():
    src = (
        b"__global__ void addKernel(int *c) {}\n"
        b"void run(int n) {\n"
        b"    addKernel<<<1, n>>>(nullptr);\n"
        b"    plainCall(n);\n"            # ordinary call -- NOT a launch
        b"}\n"
    )
    rf = parse_source(src, "k.cu")
    callees = [(caller, callee) for (caller, _line, callee) in rf.launches]
    assert callees == [("run", "addKernel")]


def test_launch_callee_name_forms():
    # bare identifier, namespaced, and member (obj.kernel) launch callees all resolve to a simple name
    src = (
        b"void run() {\n"
        b"    k1<<<1,1>>>(a);\n"
        b"    ns::k2<<<1,1>>>(b);\n"
        b"    obj.k3<<<1,1>>>(c);\n"
        b"}\n"
    )
    rf = parse_source(src, "k.cu")
    assert sorted(callee for (_c, _l, callee) in rf.launches) == ["k1", "k2", "k3"]


def test_launch_attributed_to_enclosing_method():
    src = (
        b"struct Grid {\n"
        b"  void go() { myKernel<<<2, 2>>>(nullptr); }\n"
        b"};\n"
    )
    rf = parse_source(src, "g.cu")
    assert [(c, callee) for (c, _l, callee) in rf.launches] == [("go", "myKernel")]


def test_base_cpp_structure_flows_through():
    # The shared C++ engine still extracts namespaces/types/functions from a .cu file.
    src = (
        b"namespace app {\n"
        b"struct Vec { float x; float y; };\n"
        b"__global__ void scale(Vec *v, float s) {}\n"
        b"int helper() { return 1; }\n"
        b"}\n"
    )
    rf = parse_source(src, "app.cu")
    assert any(t.name == "Vec" and t.kind == "Struct" for t in rf.types)
    fn_names = {f.name for f in rf.functions}
    assert {"scale", "helper"} <= fn_names


def test_templated_kernels_and_launches():
    # templated kernel definitions still get their execution space, and templated launch callees
    # (`reduce<float>`, `ns::scan<int, 128>`) resolve to the kernel's simple name (CUDA uses these heavily)
    src = (
        b"template <typename T>\n__global__ void reduce(T *x) {}\n"
        b"template <typename T, int N>\n__global__ void scan(T *x) {}\n"
        b"void run(float *a) {\n"
        b"    reduce<float><<<16, 256>>>(a);\n"
        b"    ns::scan<int, 128><<<1, 1>>>((int*)a);\n"
        b"}\n"
    )
    rf = parse_source(src, "t.cu")
    spaces = {name: space for (name, _line), space in rf.exec_spaces.items()}
    assert spaces == {"reduce": "global", "scan": "global"}
    assert sorted(callee for (_c, _l, callee) in rf.launches) == ["reduce", "scan"]


def test_in_class_method_prototype_qualifier():
    # a __device__ method DECLARED in-class (body out-of-line) is a field_declaration -- its qualifier
    # must still be captured (PR #31 review: common CUDA style is the qualifier on the declaration)
    src = (
        b"struct S {\n"
        b"  __device__ int f();\n"            # in-class prototype (field_declaration)
        b"  __host__ void g() {}\n"           # in-class definition (function_definition)
        b"};\n"
    )
    rf = parse_source(src, "s.cuh")
    spaces = {name: space for (name, _line), space in rf.exec_spaces.items()}
    assert spaces.get("f") == "device"
    assert spaces.get("g") == "host"


def test_plain_cpp_file_has_no_cuda_extras():
    src = b"int add(int a, int b) { return a + b; }\n"
    rf = parse_source(src, "plain.cu")
    assert rf.exec_spaces == {}
    assert rf.launches == []
    assert {f.name for f in rf.functions} == {"add"}
