"""Shared primitives for the per-language two-phase deep passes (each ingestor's `git_sync.py`).

The deep OVERLAY -- which fast, name-resolved CALLS edges a resolved SCIP pass is allowed to delete -- is a
correctness-sensitive invariant that must stay IDENTICAL across languages: drift here silently corrupts the
graph (a real call edge dropped with no replacement, or a wrong name-guess kept). That invariant lived
copied verbatim in all five deep passes; it lives here now, defined once.

Deliberately small. The larger orchestration around it (`sync_at` / `_deep_sync_at` / `_apply` / `_finish`)
genuinely differs per language -- C++ stamps `.h` headers and writes a delta-format marker, TypeScript
streams from a parse plan, Go threads a module map, Rust recomputes `unresolved_calls` -- so it stays
per-language rather than forced behind a hook-heavy abstraction."""

from __future__ import annotations

from auryn.core.graph_model import GraphNode

# Depth rank for the two-phase short-circuit: a sync requested at depth D is skipped when the graph already
# sits at the same commit with depth >= D. None/"" = never synced, "fast" = tree-sitter, "deep" = SCIP.
DEPTH_RANK = {None: 0, "": 0, "fast": 1, "deep": 2}


def project_node(proj_id: str, project_name: str, repo_uri: str, target_sha: str, ref: str | None,
                 depth: str) -> GraphNode:
    """The Project node carrying the sync's identity + `depth`. stream_apply merges props, so adding `depth`
    leaves the provenance `delta_format` marker (written separately) intact."""
    return GraphNode(
        "Project", {"id": proj_id},
        {"name": project_name, "repo_uri": repo_uri, "commit_sha": target_sha, "ref": ref, "depth": depth},
    )


def is_name_calls_edge(item) -> bool:
    """A fast pass's NAME-resolved CALLS edge -- the only kind the deep overlay may drop (a deep pass
    replaces name guesses with resolved edges; structure / extern / already-resolved edges pass through).
    Duck-typed on the class name so this stays importable wherever the graph-model item is constructed."""
    return (type(item).__name__ == "GraphEdge" and item.rel_type == "CALLS"
            and (item.properties or {}).get("resolution") == "name")


def should_drop_name_edge(item, deep, loc_by_id) -> bool:
    """Given a fast name-CALLS edge (see `is_name_calls_edge`), True if the deep pass IDENTIFIED that call,
    so the fast edge must be retired: deep either resolved it in-source (a resolved edge replaces it) or
    found it was an external callee the fast pass wrongly guessed local (e.g. `std::mem::drop` guessed as a
    local `drop`). A call deep could NOT identify (an ambiguous same-line def, a function pointer) keeps its
    fast edge. `loc_by_id` maps a node id to its CallableLocation -- `callee is None` means an extern /
    other-repo target, which is always kept. `deep` is the DeepCalls result (covered_ids + seen_by_caller)."""
    frm = item.from_key["id"]
    callee = loc_by_id.get(item.to_key["id"])
    return (frm in deep.covered_ids and callee is not None
            and callee.name in deep.seen_by_caller.get(frm, set()))
