"""tree-sitter-cuda Language/Parser setup, in one place so file_parser.py and tests always get
the exact same configured parser. Like tree-sitter-cpp/-c/-java, tree-sitter-cuda ships a
pre-built PyPI package -- no codegen/vendoring. The grammar is a strict superset of
tree-sitter-cpp: every C++ construct parses to the identical node types/fields (verified), so the
C++ extraction engine (auryn.ingestors.cpp.file_parser) runs over this parser unchanged; the only
extra node types are `kernel_call_syntax` (the `<<<...>>>` launch) and the CUDA execution-space
qualifiers (`__global__`/`__device__`/`__host__`)."""

from __future__ import annotations

import tree_sitter_cuda as tscuda
from tree_sitter import Language, Parser

LANGUAGE = Language(tscuda.language())


def new_parser() -> Parser:
    return Parser(LANGUAGE)
