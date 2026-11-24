"""Unit tests for the project-wide C resolution pass (no FalkorDB): RawFile list -> graph
nodes/edges. Focused here on the shared `doc` property (leading comment) surviving the
per-file -> graph-node transition for callables and named types."""

from __future__ import annotations

from auryn.ingestors.c import resolution
from auryn.ingestors.c.file_parser import parse_source
from auryn.ingestors.c.ids import project_id

_PROJ = project_id("demo")


def _resolve(files: dict[str, str]):
    raw = [parse_source(src.encode(), path) for path, src in files.items()]
    items = list(resolution.resolve(raw, _PROJ))
    return [i for i in items if type(i).__name__ == "GraphNode"]


def _by_name(nodes, label, name):
    return next(n for n in nodes if n.label == label and n.properties.get("name") == name)


def _edges(files: dict[str, str]):
    raw = [parse_source(src.encode(), path) for path, src in files.items()]
    items = list(resolution.resolve(raw, _PROJ))
    return [i for i in items if type(i).__name__ == "GraphEdge"]


def test_calls_deduped_per_caller_callee():
    """One CALLS edge per (caller, callee) pair even when the call appears at several sites -- a
    graph models 'a calls b', not the site count, and parallel edges share the (type,from,to) key
    the delta reconciler diffs on, so an unreconcilable multiplicity change would strand a stale
    duplicate on resync. Holds for both resolved callees and extern:// placeholders."""
    edges = _edges({
        "a.c": """
        int helper(int x) { return x; }
        int run(void) {
            helper(1); helper(2); helper(3);     /* resolved: 3 sites -> 1 edge */
            missing_fn(); missing_fn();          /* extern:   2 sites -> 1 edge */
            return 0;
        }
        """
    })
    calls = [e for e in edges if e.rel_type == "CALLS"
             and e.from_key["id"].endswith("/run")]
    targets = [e.to_key["id"] for e in calls]
    assert sorted(targets) == [f"{_PROJ}/func/helper", "extern://c/missing_fn"]


def test_doc_property_reaches_nodes():
    nodes = _resolve({
        "widget.c": """
        // widget_new allocates a Widget.
        struct Widget *widget_new(void) { return 0; }
        // Widget is a drawable thing.
        struct Widget { int id; };
        /*
         * handle_t is an opaque handle.
         */
        typedef struct Widget *handle_t;
        // Color enumerates the palette.
        enum Color { RED, GREEN };
        """
    })
    assert _by_name(nodes, "Function", "widget_new").properties["doc"] == "widget_new allocates a Widget."
    assert _by_name(nodes, "Struct", "Widget").properties["doc"] == "Widget is a drawable thing."
    assert _by_name(nodes, "TypeAlias", "handle_t").properties["doc"] == "handle_t is an opaque handle."
    assert _by_name(nodes, "Enum", "Color").properties["doc"] == "Color enumerates the palette."
