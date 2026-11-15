"""CUDA resolution. CUDA is a C++ superset, so the base graph (types, methods, functions, params,
hierarchy, calls, includes, DEFINES/DECLARES provenance) is produced by the shared C++ resolver run
with a `cuda://` project root -- cpp.resolution.resolve roots every id on the project id it is
handed, so passing a cuda root flows it through unchanged, no fork.

This wrapper layers the two CUDA-only signals on top, without touching the C++ resolver:

  * execution_space -- as each Function/Method node streams by, the property is set on the node in
    place (the node's own `properties` dict is mutable even though GraphNode is frozen). It is ALWAYS
    written -- the detected space for a kernel/device/host function, or None otherwise -- so a `+=`
    merge clears a stale value if a callable loses its qualifier across a resync (a bare `+=` would
    otherwise keep the old value, since merge never removes absent keys).

  * LAUNCHES edges -- after the base stream, each recorded `kernel<<<...>>>(...)` launch becomes a
    LAUNCHES edge from the enclosing function/method to the launched __global__ kernel. The target is
    taken from the CALLS edge the C++ resolver already resolved for that same launch call (a launch IS
    a call_expression), so LAUNCHES uses the identical scope-aware endpoint as CALLS -- e.g. a bare
    `k<<<>>>()` in namespace `b` resolves to `b::k`, not some other `a::k`. Only when the C++ resolver
    produced no CALLS for the callee (today: a templated callee like `reduce<T>`, which cpp's call
    extractor does not yet capture -- a pre-existing limitation that also drops the plain CALLS edge)
    does it fall back to a project-wide __global__-kernel-by-simple-name lookup. No edge is emitted
    when the kernel is not resolvable in this graph (no edge rather than a wrong one).

Delta-sync note: v1 is full-recompute (git_sync does not route incremental). An incremental engine
(CUDA_SPEC) is a planned follow-up; it would need the same augmentation in resolve_incremental."""

from __future__ import annotations

from collections.abc import Iterator

from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.ingestors.cpp import resolution as cpp_resolution

_CALLABLE_LABELS = ("Function", "Method")


def resolve(raw_files: list, project_id: str) -> Iterator[GraphNode | GraphEdge]:
    exec_by_key: dict = {}        # (name, line_start) -> execution space
    launches: list = []           # (caller_name, caller_line_start, callee_name)
    for rf in raw_files:
        exec_by_key.update(getattr(rf, "exec_spaces", {}) or {})
        launches.extend(getattr(rf, "launches", []) or [])

    id_by_key: dict = {}          # (name, line_start) -> (node id, node label) for callers
    node_info: dict = {}          # node id -> (name, execution_space) for every callable
    kernel_ids_by_name: dict = {} # kernel simple name -> {node ids} (fallback launch targets)
    calls_by_caller: dict = {}    # caller id -> set of callee ids (the C++ resolver's resolved CALLS)

    for item in cpp_resolution.resolve(raw_files, project_id):
        if isinstance(item, GraphNode) and item.label in _CALLABLE_LABELS:
            name = item.properties.get("name")
            key = (name, item.properties.get("line_start"))
            space = exec_by_key.get(key)
            # ALWAYS write execution_space (None clears a stale value on `+=` merge across a resync).
            item.properties["execution_space"] = space
            nid = item.key["id"]
            id_by_key[key] = (nid, item.label)
            node_info[nid] = (name, space)
            if space == "global":
                kernel_ids_by_name.setdefault(name, set()).add(nid)
        elif isinstance(item, GraphEdge) and item.rel_type == "CALLS":
            calls_by_caller.setdefault(item.from_key["id"], set()).add(item.to_key["id"])
        yield item

    # LAUNCHES edges, once every node/edge (and so every id + resolved CALLS) has been seen.
    seen: set = set()
    for caller_name, caller_line, callee_name in launches:
        caller = id_by_key.get((caller_name, caller_line))
        if caller is None:
            continue
        caller_id, caller_label = caller
        # Prefer the kernel the C++ resolver already picked for this launch's CALLS edge (scope-aware).
        target = next(
            (cid for cid in calls_by_caller.get(caller_id, ())
             if node_info.get(cid) == (callee_name, "global")),
            None,
        )
        if target is None:
            # Fall back to a simple-name __global__ lookup only when there was no CALLS (templated
            # callee cpp didn't resolve) AND the name is UNAMBIGUOUS -- emit no edge rather than a
            # wrong one when two namespaces hold a same-named kernel (the launch's scope was lost).
            candidates = kernel_ids_by_name.get(callee_name, ())
            target = next(iter(candidates)) if len(candidates) == 1 else None
        if target is None:
            continue
        dedup = (caller_id, target)
        if dedup in seen:
            continue
        seen.add(dedup)
        yield GraphEdge(
            "LAUNCHES", caller_label, {"id": caller_id}, "Function", {"id": target},
            {"resolution": "name"},
        )
