"""CUDA file parsing. CUDA is a strict superset of C++, so the whole C++ structure (namespaces,
types, methods, free functions, fields, params, calls, includes, macros, out-of-line defs) is
produced by the shared tree-sitter-cpp extraction engine run over the tree-sitter-cuda parser --
no duplication (see auryn.ingestors.cpp.file_parser.parse_source, which accepts an injected parser).

On top of that this module extracts the two CUDA-only signals the C++ grammar has no concept of:

  * execution space -- the `__global__` / `__device__` / `__host__` qualifier on a function or
    method (direct named children of its `function_definition` / `declaration`). Keyed by
    (name, line_start) so resolution can attach it to the Function/Method node the C++ resolver
    emits (which carries the same name + line_start), as an `execution_space` property.
  * kernel launches -- a `kernel<<<grid, block>>>(args)` site, parsed as a `call_expression` that
    has a `kernel_call_syntax` child. Recorded as (enclosing function, launched kernel name) so
    resolution can emit a LAUNCHES edge. (The launch is ALSO an ordinary CALLS edge -- it is a
    call_expression -- so LAUNCHES is a typed overlay marking the host->device boundary, not a
    replacement.)"""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

from auryn.ingestors.cpp.file_parser import parse_source as _cpp_parse_source
from auryn.ingestors.cuda.grammar import new_parser
from auryn.ingestors.cuda.raw_model import CudaRawFile

# node.type -> execution space token. A function may carry several (e.g. __host__ __device__).
_EXEC_NODE_TYPES = {"__global__", "__device__", "__host__"}
_NAME_NODES = {"identifier", "field_identifier", "destructor_name", "operator_name", "type_identifier"}
# Nodes a function/method declaration can be: a free function (`function_definition`/`declaration`)
# or an in-class member -- a `field_declaration` for a prototype (`__device__ void f();`) that the
# shared C++ engine emits as a Method. Including it means a qualifier on an in-class DECLARATION is
# captured even when the out-of-line definition omits it (a common CUDA style).
_DECL_NODES = {"function_definition", "declaration", "field_declaration"}


def parse_file(path: str) -> CudaRawFile:
    return parse_source(Path(path).read_bytes(), path)


def parse_source(source: bytes, path_label: str) -> CudaRawFile:
    parser = new_parser()
    # 1. Full C++ graph via the shared engine, parsed with the CUDA (superset) grammar.
    base = _cpp_parse_source(source, path_label, parser=parser)
    # 2. CUDA-only side-cars from a walk of the same source.
    exec_spaces, launches = _extract_cuda_extras(source, parser)
    return CudaRawFile(
        **{f.name: getattr(base, f.name) for f in fields(base)},
        exec_spaces=exec_spaces,
        launches=launches,
    )


def _extract_cuda_extras(source: bytes, parser):
    """(exec_spaces, launches) for one CUDA translation unit. See the module docstring for the keys."""
    tree = parser.parse(source)

    def txt(node) -> str:
        return source[node.start_byte : node.end_byte].decode("utf-8", "replace")

    def final_segment(qid):
        # `A::B::deep` -> "deep"; descend the `name` field past every qualifier level. A templated
        # final segment (`A::scan<int>`) unwraps to the template's own name ("scan").
        node = qid
        while node is not None and node.type == "qualified_identifier":
            node = node.child_by_field_name("name")
        if node is not None and node.type == "template_function":
            node = node.child_by_field_name("name")
        return txt(node) if node is not None else None

    def function_declarator(node):
        # Descend declarator wrappers (pointer/reference/...) to the function_declarator, if any.
        decl = node.child_by_field_name("declarator")
        seen = 0
        while decl is not None and seen < 64:
            if decl.type == "function_declarator":
                return decl
            nxt = decl.child_by_field_name("declarator")
            if nxt is None:
                nxt = next((c for c in decl.children if c.type.endswith("declarator")), None)
            decl = nxt
            seen += 1
        return None

    def declared_name(fdecl):
        d = fdecl.child_by_field_name("declarator")
        if d is None:
            d = next((c for c in fdecl.children if c.type in _NAME_NODES or c.type == "qualified_identifier"), None)
        if d is None:
            return None
        if d.type == "qualified_identifier":
            return final_segment(d)
        return txt(d) if d.type in _NAME_NODES else None

    def exec_space(node):
        spaces = {c.type for c in node.children if c.type in _EXEC_NODE_TYPES}
        if not spaces:
            return None
        if "__global__" in spaces:
            return "global"
        if "__device__" in spaces and "__host__" in spaces:
            return "host_device"
        if "__device__" in spaces:
            return "device"
        return "host"  # only __host__

    def callee_name(fn_node):
        if fn_node is None:
            return None
        if fn_node.type in _NAME_NODES:
            return txt(fn_node)
        if fn_node.type == "qualified_identifier":
            return final_segment(fn_node)
        if fn_node.type == "template_function":  # templated kernel `reduce<float><<<...>>>(...)`
            nm = fn_node.child_by_field_name("name")
            return txt(nm) if nm is not None else None
        if fn_node.type == "field_expression":  # obj.kernel / ptr->kernel
            fld = fn_node.child_by_field_name("field")
            return txt(fld) if fld is not None else None
        return None

    exec_spaces: dict = {}
    launches: list = []

    # Iterative DFS carrying the enclosing (function name, line_start) so a launch is attributed to
    # the function it sits in. Order is irrelevant -- everything is collected into the maps.
    stack = [(tree.root_node, None)]
    while stack:
        node, enclosing = stack.pop()
        enc = enclosing
        if node.type in _DECL_NODES:
            fdecl = function_declarator(node)
            if fdecl is not None:
                name = declared_name(fdecl)
                line = node.start_point[0] + 1
                if name:
                    space = exec_space(node)
                    if space:
                        exec_spaces[(name, line)] = space
                    if node.type == "function_definition":
                        enc = (name, line)  # body launches are attributed to this function
        elif node.type == "call_expression" and any(c.type == "kernel_call_syntax" for c in node.children):
            callee = callee_name(node.child_by_field_name("function"))
            if callee and enc is not None:
                launches.append((enc[0], enc[1], callee))
        for c in node.children:
            stack.append((c, enc))

    return exec_spaces, launches
