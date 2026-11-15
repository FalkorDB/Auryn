"""Canonical id root for the CUDA graph.

CUDA reuses the C++ id scheme wholesale (namespace tier over types-with-members, overloads keyed by
parameter-type text, templates by arity) -- see auryn.ingestors.cpp.ids. The only CUDA-specific
piece is the project root: nodes are keyed `cuda://<project>/...` so a CUDA subgraph stays distinct
from a C or C++ subgraph of the same name in one graph (the per-language id-root convention every
ingestor follows). Every downstream id builder in cpp.ids takes the project/scope id as an argument,
so passing this root flows it through the whole C++ resolution unchanged."""

from __future__ import annotations


def project_id(project_name: str) -> str:
    return f"cuda://{project_name}"
