"""Best-effort extraction of the Kendo/ASP.NET MVC view tier from a repo's `.cshtml`
files, run as a stage of C# git-sync so its nodes join the same `csharp://<project>`
subgraph as the C# code and its seam edges land on the real C# `Method` nodes.

This is the *start* of the golden path (Kendo UI event -> C# -> ...), so what
matters most is the seam: a grid's data-source / a form's post target resolved to the
controller action it calls. Views mix HTML + Razor + Kendo HtmlHelpers *and/or* client
JS with no off-the-shelf semantic parser, so this is deliberately pattern-based and
best-effort (like the PL/SQL parser: it never fails a sync; an unresolved reference
just drops that one edge, keeping the node). Patterns cover the common Kendo MVC
shapes and will need tuning against real App.WEB views.

Entities (doc S4/A.1, rooted under the C# project id):
  View  -> Grid -> Field, and View -> EventHandler       (CONTAINS)
  EventHandler -HANDLES-> Grid                            (event name as a property)
  Grid -DISPLAYS_DATA-> Method                            (the read action)
  Grid -CALLS_API-> Method                                (update/create/destroy)
  View -SUBMITS_TO-> Method                               (a form's post target)
"""

from __future__ import annotations

import logging
import re

from auryn.core.git_source import GitRepoCache
from auryn.core.graph_model import GraphEdge, GraphNode

logger = logging.getLogger(__name__)

_EXCLUDED_DIR_NAMES = {"bin", "obj"}

# -- Razor HtmlHelper patterns ------------------------------------------------------
_GRID_MARKER = re.compile(r"Html\.Kendo\(\)\.Grid")
_NAME = re.compile(r'\.Name\(\s*"([^"]+)"')
_BOUND_COLUMN = re.compile(r'\.Bound\(\s*\w+\s*=>\s*\w+\.(\w+)')
_FIELD_COLUMN = re.compile(r'\.Field\(\s*"([^"]+)"')
_TRANSPORT = re.compile(
    r'\.(Read|Update|Create|Destroy)\(\s*\w+\s*=>\s*\w+\.Action\(\s*"([^"]+)"\s*(?:,\s*"([^"]+)")?\s*\)'
)
_BEGIN_FORM = re.compile(r'\.BeginForm\(\s*"([^"]+)"\s*(?:,\s*"([^"]+)")?')
_EVENT_IN_CHAIN = re.compile(r'\.(\w+)\(\s*"([^"]+)"\s*\)')
# Navigation / child-action refs from a view: `@Html.ActionLink("text","Action"[,"Controller"])`
# and `@Html.Action("Action"[,"Controller"])` / `@Html.RenderAction(...)` (server-side child action).
# Both invoke a controller action, so they're a golden-path seam (View -CALLS_API-> Method). The
# `Html.` prefix distinguishes them from a grid transport's `r.Action(...)` and from `@Url.Action`.
_ACTION_LINK = re.compile(r'@?Html\.ActionLink\(\s*"[^"]*"\s*,\s*"([^"]+)"\s*(?:,\s*"([^"]+)")?')
_HTML_ACTION = re.compile(r'@?Html\.(?:Render)?Action\(\s*"([^"]+)"\s*(?:,\s*"([^"]+)")?')

# -- Client-JS patterns -------------------------------------------------------------
_JS_GRID = re.compile(r'\$\(\s*["\']#([^"\']+)["\']\s*\)\.kendoGrid\(')
_JS_FIELD = re.compile(r'field\s*:\s*["\']([^"\']+)["\']')
_JS_TRANSPORT = re.compile(
    r'(read|update|create|destroy)\s*:\s*(?:\{[^{}]*?url\s*:\s*)?["\']([^"\']+)["\']'
)
# A transport whose url is the Razor `@Url.Action("Action"[,"Controller"])` helper -- the
# dominant idiom. It's matched separately because the helper's own quotes nest inside the JS
# string quotes, so `_JS_TRANSPORT` above captures only up to `@Url.Action(` and drops it.
_JS_TRANSPORT_URLACTION = re.compile(
    r'(read|update|create|destroy)\s*:\s*(?:\{[^{}]*?url\s*:\s*)?[\'"]?\s*@?Url\.Action\('
    r'\s*"([^"]+)"\s*(?:,\s*"([^"]+)")?\s*\)',
    re.IGNORECASE,
)
# `@Url.Action("Action"[, "Controller"])` anywhere in a captured URL string.
_URL_ACTION = re.compile(r'@?Url\.Action\(\s*"([^"]+)"\s*(?:,\s*"([^"]+)")?\s*\)', re.IGNORECASE)
_JS_EVENT = re.compile(r'\b(change|save|edit|remove|dataBound|dataBinding)\s*:\s*(\w+)')

_TRANSPORT_EDGE = {"read": "DISPLAYS_DATA", "update": "CALLS_API", "create": "CALLS_API", "destroy": "CALLS_API"}


def extract_ui(
    repo: GitRepoCache, target_sha: str, proj_id: str, csharp_items: list[GraphNode | GraphEdge]
) -> list[GraphNode | GraphEdge]:
    """Parse every `.cshtml` at `target_sha` into UI nodes + seam edges onto the C#
    `Method` nodes present in `csharp_items`. Returns [] when there are no views."""
    view_files = repo.list_files(target_sha, ".cshtml", _EXCLUDED_DIR_NAMES)
    if not view_files:
        return []

    actions = _build_action_index(csharp_items)
    items: list[GraphNode | GraphEdge] = []
    for path, blob_sha in view_files:
        source = repo.read_blob(blob_sha).decode("utf-8", errors="replace")
        items.extend(_extract_view(path, source, proj_id, actions))
    logger.info("Kendo/MVC: parsed %d view(s), emitted %d items", len(view_files), len(items))
    return items


# -- Action index (controller, action) -> C# Method id ------------------------------


def _build_action_index(csharp_items: list[GraphNode | GraphEdge]) -> dict[tuple[str, str], str]:
    """Map (controller-key, action-name) -> Method id, reusing the ids the C# engine
    already assigned. A controller is a `Class` whose name ends `Controller`; its
    actions are the `Method` nodes whose id nests under the class's id."""
    controllers: dict[str, str] = {}  # class type_id -> controller key ("widgets")
    for item in csharp_items:
        if isinstance(item, GraphNode) and item.label == "Class":
            name = str(item.properties.get("name", ""))
            if name.endswith("Controller"):
                controllers[item.key["id"]] = name[: -len("Controller")].lower()

    index: dict[tuple[str, str], str] = {}
    for item in csharp_items:
        if isinstance(item, GraphNode) and item.label == "Method":
            method_id = item.key["id"]
            # An action is reachable by its C# method name AND, when present, its
            # `[ActionName("X")]` override -- MVC routing (Url.Action, grid transports) targets X.
            names = {str(item.properties.get("name", "")).lower()}
            action_name = item.properties.get("action_name")
            if action_name:
                names.add(str(action_name).lower())
            for type_id, controller_key in controllers.items():
                if method_id.startswith(f"{type_id}.method."):
                    for name in names:
                        index.setdefault((controller_key, name), method_id)
                    break
    return index


# -- Per-view extraction ------------------------------------------------------------


def _extract_view(path: str, source: str, proj_id: str, actions: dict[tuple[str, str], str]) -> list[GraphNode | GraphEdge]:
    view_id = f"{proj_id}/view/{path}"
    default_controller = _view_controller(path)
    items: list[GraphNode | GraphEdge] = [
        GraphNode("View", {"id": view_id}, {"name": path.rsplit("/", 1)[-1], "path": path, "provenance": "view"})
    ]

    for name, block in _grids(source):
        items.extend(_emit_grid(view_id, name, block, source, default_controller, actions))

    # Forms: (View)-[:SUBMITS_TO]->(Method)
    for action, controller in _BEGIN_FORM.findall(source):
        target = actions.get(((controller or default_controller).lower(), action.lower()))
        if target is not None:
            items.append(GraphEdge("SUBMITS_TO", "View", {"id": view_id}, "Method", {"id": target}))

    # Nav / child-action refs: (View)-[:CALLS_API]->(Method), de-duped per view.
    seen_targets: set[str] = set()
    for action, controller in _view_action_refs(source):
        target = actions.get(((controller or default_controller).lower(), action.lower()))
        if target is not None and target not in seen_targets:
            seen_targets.add(target)
            items.append(GraphEdge("CALLS_API", "View", {"id": view_id}, "Method", {"id": target}, {"provenance": "view"}))
    return items


def _emit_grid(
    view_id: str, name: str, block: str, full_source: str, default_controller: str, actions: dict[tuple[str, str], str]
) -> list[GraphNode | GraphEdge]:
    grid_id = f"{view_id}.grid.{name.lower()}"
    items: list[GraphNode | GraphEdge] = [
        GraphNode("Grid", {"id": grid_id}, {"name": name, "provenance": "view"}),
        GraphEdge("CONTAINS", "View", {"id": view_id}, "Grid", {"id": grid_id}),
    ]

    seen_fields: set[str] = set()
    for field in _BOUND_COLUMN.findall(block) + _FIELD_COLUMN.findall(block) + _JS_FIELD.findall(block):
        if field in seen_fields:
            continue
        seen_fields.add(field)
        field_id = f"{grid_id}.field.{field.lower()}"
        items.append(GraphNode("Field", {"id": field_id}, {"name": field, "provenance": "view"}))
        items.append(GraphEdge("CONTAINS", "Grid", {"id": grid_id}, "Field", {"id": field_id}))

    for operation, action, controller in _transports(block):
        target = actions.get(((controller or default_controller).lower(), action.lower()))
        if target is None:
            continue  # unresolved action -- keep the grid, drop just this seam edge
        rel = _TRANSPORT_EDGE[operation]
        props = {"provenance": "view"} if operation == "read" else {"provenance": "view", "operation": operation}
        items.append(GraphEdge(rel, "Grid", {"id": grid_id}, "Method", {"id": target}, props))

    for event, handler in _events(block):
        handler_id = f"{grid_id}.event.{event.lower()}"
        items.append(GraphNode("EventHandler", {"id": handler_id}, {"name": handler, "event": event, "provenance": "view"}))
        items.append(GraphEdge("CONTAINS", "View", {"id": view_id}, "EventHandler", {"id": handler_id}))
        items.append(GraphEdge("HANDLES", "EventHandler", {"id": handler_id}, "Grid", {"id": grid_id}, {"event": event}))
    return items


# -- Grid discovery (both styles) ---------------------------------------------------


def _grids(source: str):
    """Yield (grid_name, block) for each Kendo grid, HtmlHelper or client-JS."""
    markers = [m.start() for m in _GRID_MARKER.finditer(source)]
    for i, start in enumerate(markers):
        end = markers[i + 1] if i + 1 < len(markers) else len(source)
        block = source[start:end]
        name_match = _NAME.search(block)
        yield (name_match.group(1) if name_match else f"grid{i}"), block

    for match in _JS_GRID.finditer(source):
        block = _paren_group(source, match.end() - 1)
        yield match.group(1), block


def _transports(block: str) -> list[tuple[str, str, str | None]]:
    """(operation, action, controller|None) for every data-source transport op, de-duped."""
    out: list[tuple[str, str, str | None]] = []
    for operation, action, controller in _TRANSPORT.findall(block):  # HtmlHelper
        out.append((operation.lower(), action, controller or None))
    for operation, action, controller in _JS_TRANSPORT_URLACTION.findall(block):  # client JS: @Url.Action(...)
        out.append((operation.lower(), action, controller or None))
    for operation, url in _JS_TRANSPORT.findall(block):  # client JS: "/Controller/Action"
        parsed = _controller_action_from_url(url)
        if parsed is not None:
            out.append((operation.lower(), parsed[1], parsed[0]))
    # De-dupe: @Url.Action urls match both the dedicated pattern (correctly) and _JS_TRANSPORT
    # (as a dropped None); a clean path never overlaps. Preserve first-seen order.
    seen: set[tuple[str, str, str | None]] = set()
    deduped: list[tuple[str, str, str | None]] = []
    for t in out:
        if t not in seen:
            seen.add(t)
            deduped.append(t)
    return deduped


def _view_action_refs(source: str) -> list[tuple[str, str | None]]:
    """(action, controller|None) for every `@Html.ActionLink`/`@Html.Action`/`@Html.RenderAction`
    in a view -- nav links and server-side child actions that target a controller action."""
    refs: list[tuple[str, str | None]] = []
    for action, controller in _ACTION_LINK.findall(source):
        refs.append((action, controller or None))
    for action, controller in _HTML_ACTION.findall(source):
        refs.append((action, controller or None))
    return refs


def _events(block: str) -> list[tuple[str, str]]:
    """(event, handler) pairs -- HtmlHelper `.Events(e => e.Change("fn"))` and client
    JS `change: fn`."""
    events: list[tuple[str, str]] = []
    marker = block.find(".Events(")
    if marker != -1:
        group = _paren_group(block, block.index("(", marker))
        events.extend((event, handler) for event, handler in _EVENT_IN_CHAIN.findall(group))
    events.extend((event, handler) for event, handler in _JS_EVENT.findall(block))
    return events


# -- Helpers ------------------------------------------------------------------------


def _paren_group(text: str, open_paren_idx: int) -> str:
    """The substring inside the parenthesis group that opens at `open_paren_idx`,
    matched with balanced depth (so a nested config object is captured whole)."""
    depth = 0
    for i in range(open_paren_idx, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren_idx + 1 : i]
    return text[open_paren_idx + 1 :]


def _view_controller(path: str) -> str:
    """Default controller for a view lacking an explicit one in an Action ref -- the
    MVC convention `Views/<Controller>/<Action>.cshtml`."""
    parts = path.replace("\\", "/").split("/")
    if "Views" in parts:
        idx = parts.index("Views")
        if idx + 1 < len(parts) - 1:
            return parts[idx + 1].lower()
    return parts[-2].lower() if len(parts) >= 2 else ""


def _controller_action_from_url(url: str) -> tuple[str | None, str] | None:
    """`/Widgets/List` -> ("Widgets", "List"); `@Url.Action("List","Widgets")` ->
    ("Widgets", "List") and `@Url.Action("List")` -> (None, "List") (default controller);
    ignores absolute/external URLs."""
    action_match = _URL_ACTION.search(url)
    if action_match:
        action, controller = action_match.group(1), action_match.group(2)
        return (controller or None), action
    if "://" in url:
        return None
    segments = [s for s in url.split("?")[0].split("/") if s]
    if len(segments) >= 2:
        return segments[-2], segments[-1]
    return None
