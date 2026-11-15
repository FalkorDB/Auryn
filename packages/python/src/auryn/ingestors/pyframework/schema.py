"""Graph schema for the Python web-framework enrichment.

Reuses the C# tier's `APIEndpoint` label + `EXPOSES_ENDPOINT` edge on purpose, so a Python
route is a first-class entry point to the same agent-access tools (trace_entrypoint,
search by APIEndpoint) that already understand endpoints -- see synthesize.py. The Python
endpoints carry `provenance:"pyframework"` so this pass can recompute only its own layer
without touching any C# APIEndpoints in the same graph.
"""

from __future__ import annotations

from auryn.core.graph_model import EdgeSchema, NodeSchema

NODE_SCHEMAS = [
    NodeSchema(label="APIEndpoint", indexed_properties=("route",)),
]

EDGE_SCHEMAS = [
    EdgeSchema(rel_type="EXPOSES_ENDPOINT"),  # handler Function|Method -> APIEndpoint
]
