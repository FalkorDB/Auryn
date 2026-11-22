"""The deep-pass OVERLAY invariant, shared by every language's deep `git_sync`.

Which fast, name-resolved `CALLS` edges a resolved SCIP pass is allowed to DELETE is correctness-sensitive:
drift across languages silently corrupts the graph (a real call edge dropped with no deep replacement, or a
wrong name-guess kept). That logic lived copied verbatim in all five deep passes; it lives here now, once.

Imported lazily on the DEEP path only (inside each `_deep_sync_at`, behind the same `ImportError` guard as
`core.scip_resolve`), so a plugin-only upgrade against an older core degrades to the fast pass rather than
breaking it -- the fast pass never imports this module. The rest of the two-phase shell (`sync_at`,
`_deep_sync_at`, `_apply`, `_finish`, and the trivial `_DEPTH_RANK` / `_project_node`) genuinely differs per
language and stays there."""

from __future__ import annotations


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
