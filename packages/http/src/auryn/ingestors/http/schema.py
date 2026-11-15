"""Graph schema the HTTP stitch touches (ADR 0006). The APIEndpoint node + EXPOSES_ENDPOINT edge
are shared with the language ingestors; declared here so the hook can ensure the route index it
seeks on exists even when it runs first."""

from __future__ import annotations

from auryn.core.graph_model import EdgeSchema, NodeSchema

NODE_SCHEMAS = [
    NodeSchema(label="APIEndpoint", indexed_properties=("route",)),
]

EDGE_SCHEMAS = [
    EdgeSchema(rel_type="EXPOSES_ENDPOINT"),  # handler Method -> APIEndpoint (provenance http-stitch)
]
