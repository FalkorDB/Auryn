"""Graph schema the CUDA ingestor writes. CUDA is the structural superset of C++, so the schema IS
the C++ schema (same labels: Namespace/Class/Struct/Union/Enum/Function/Method/Field/Parameter/
EnumMember/Macro, same containment + EXTENDS/CALLS/DEPENDS_ON/INCLUDES/DEFINES/DECLARES edges) plus
two CUDA-only additions:

  * `execution_space` -- an indexed property on Function/Method recording where the code runs:
    "global" (a __global__ kernel), "device", "host", or "host_device" (__host__ __device__). Indexed
    so "find every kernel" is an index seek. Absent on ordinary (unqualified) host code.
  * LAUNCHES -- Function|Method -> Function edge for a `kernel<<<grid, block>>>(args)` launch. The
    CUDA analogue of CALLS across the host->device boundary (a plain `foo(args)` call is still CALLS;
    only the triple-angle-bracket launch is LAUNCHES).

The shared cross-ingestor contract is honored exactly (callables are Function/Method, the call edge
is CALLS with a `resolution` provenance, Parameter/HAS_PARAMETER/EXTENDS/DEPENDS_ON/DEFINED_IN match
the C/C++/Java/C#/Python ingestors); CUDA nodes differ only by the `cuda://` id root and the two
additions above."""

from __future__ import annotations

from auryn.core.graph_model import EdgeSchema, NodeSchema
from auryn.ingestors.cpp.schema import EDGE_SCHEMAS as _CPP_EDGE_SCHEMAS
from auryn.ingestors.cpp.schema import NODE_SCHEMAS as _CPP_NODE_SCHEMAS
from auryn.ingestors.cpp.schema import TYPE_LABELS  # re-exported for parity with cpp

# CUDA extras folded onto the callable labels. Everything else is the C++ schema verbatim.
_EXTRA_INDEXED = {
    "Function": ("name", "linkage_key", "execution_space"),
    "Method": ("name", "execution_space"),
}

NODE_SCHEMAS = [
    NodeSchema(label=s.label, key_property=s.key_property,
               indexed_properties=_EXTRA_INDEXED.get(s.label, s.indexed_properties))
    for s in _CPP_NODE_SCHEMAS
]

EDGE_SCHEMAS = [*_CPP_EDGE_SCHEMAS, EdgeSchema(rel_type="LAUNCHES")]  # host -> __global__ kernel

__all__ = ["NODE_SCHEMAS", "EDGE_SCHEMAS", "TYPE_LABELS"]
