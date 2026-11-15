"""Phase 2 of the C# ingestor pipeline (see the plan's "Extraction pipeline"
section): runs sequentially in the main process, once every file has been parsed in
parallel by file_parser.parse_file(). Builds a project-wide type/method index, then
uses it to resolve everything that needs cross-file information -- EXTENDS vs
IMPLEMENTS, API endpoint detection, DTO classification, and best-effort call/type-
reference resolution -- before finally yielding the graph node/edge stream.

Resolution here is deliberately best-effort and name-based (no real symbol table, no
using-directive alias resolution), consistent with the syntax-only trade-off already
accepted for this parser (see grammar.py) and with the PL/SQL ingestor's own
best-effort call resolution (source_analysis.py / ingestor.py's _resolve_call).
Ambiguous or unresolved references simply don't produce an edge, rather than
guessing wrong.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from auryn.core import http_seam
from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.ingestors.csharp import ids
from auryn.ingestors.csharp.raw_model import (
    RawAttribute,
    RawCall,
    RawEnumMember,
    RawEvent,
    RawField,
    RawFile,
    RawMethod,
    RawParameter,
    RawProperty,
    RawType,
)

_KIND_SLUG = {
    "Class": "class",
    "Interface": "interface",
    "Struct": "struct",
    "Enum": "enum",
    "Record": "record",
    "Delegate": "delegate",
}

_HTTP_VERB_ATTRIBUTES = {
    "HttpGet": "GET",
    "HttpPost": "POST",
    "HttpPut": "PUT",
    "HttpDelete": "DELETE",
    "HttpPatch": "PATCH",
    "HttpHead": "HEAD",
    "HttpOptions": "OPTIONS",
}
_CONTROLLER_BASE_NAMES = {"Controller", "ControllerBase"}
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass
class _TypeEntry:
    type_id: str
    kind: str  # RawType.kind, as literally declared -- never DTO (that's a label overlay)
    name: str
    namespace: str
    raw_type: RawType
    file_id: str
    parent_type_id: str | None
    scope_id: str
    scope_label: str | None  # "Namespace" | "Project" (top-level only; None if nested)
    qualified_name: str  # dotted name a consumer writes for this type, e.g. "Acme.Widgets.Repo"
    #                      (namespace + nesting chain) -- the root of every method linkage key.


def resolve(raw_files: list[RawFile], project_id: str, dto_name_suffixes: tuple[str, ...]) -> Iterator[GraphNode | GraphEdge]:
    file_ids: dict[str, str] = {}
    namespaces_seen: set[str] = set()
    for raw_file in raw_files:
        file_id = f"{project_id}/file/{raw_file.path}"
        file_ids[raw_file.path] = file_id
        yield GraphNode("File", {"id": file_id}, {"name": Path(raw_file.path).name, "path": raw_file.path})
        yield GraphEdge("HAS_FILE", "Project", {"id": project_id}, "File", {"id": file_id})
        for raw_type in raw_file.types:
            _collect_namespaces(raw_type, namespaces_seen)
    yield from _emit_namespaces(namespaces_seen, project_id)

    entries: list[_TypeEntry] = []
    for raw_file in raw_files:
        file_id = file_ids[raw_file.path]
        for raw_type in raw_file.types:
            _flatten_type(raw_type, file_id, project_id, None, entries)

    type_index: dict[str, list[_TypeEntry]] = defaultdict(list)
    for entry in entries:
        type_index[entry.name].append(entry)

    method_index: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for entry in entries:
        for method in entry.raw_type.methods:
            method_index[method.name].append((entry.type_id, _method_id(entry.type_id, method), entry.namespace))

    controller_like = _compute_controller_like(entries, type_index)
    definitive_dto_names = _definitive_dto_names(entries, controller_like)
    final_label_by_type_id = {
        entry.type_id: _final_label(entry, definitive_dto_names, dto_name_suffixes) for entry in entries
    }

    # Pass 1: every node this ingestor owns, plus purely-structural edges (HAS_TYPE/
    # HAS_NESTED_TYPE/DEFINED_IN/HAS_METHOD/etc.) -- so pass 2's cross-references
    # always have somewhere to land, same two-pass reasoning as the SQL/PL/SQL
    # ingestors' FK/DEPENDS_ON ordering fixes.
    for entry in entries:
        yield from _emit_type_and_members(entry, final_label_by_type_id)

    # Pass 2: cross-references (EXTENDS/IMPLEMENTS, DEPENDS_ON, API endpoints). `emitted_externs`
    # dedups ADR 0004 placeholder Method nodes across the whole project (many callers can share
    # one external/BCL callee -- they MERGE onto a single placeholder node).
    emitted_externs: set[str] = set()
    for entry in entries:
        yield from _emit_extends_implements(entry, type_index, final_label_by_type_id)
        yield from _emit_type_references(entry, type_index, final_label_by_type_id)
        yield from _emit_calls(entry, method_index, type_index, emitted_externs)
        yield from _emit_endpoints(entry, controller_like, type_index, final_label_by_type_id)


# -- Namespace / File --------------------------------------------------------------


def _collect_namespaces(raw_type: RawType, seen: set[str]) -> None:
    seen.add(raw_type.namespace)
    for nested in raw_type.nested_types:
        _collect_namespaces(nested, seen)


def _namespace_id(namespace: str, project_id: str) -> str:
    return f"{project_id}/ns/{namespace.lower()}" if namespace else project_id


def _emit_namespaces(namespaces: set[str], project_id: str) -> Iterator[GraphNode | GraphEdge]:
    emitted: set[str] = set()
    for namespace in namespaces:
        if not namespace:
            continue
        prefix = ""
        parent_id, parent_label = project_id, "Project"
        for segment in namespace.split("."):
            prefix = f"{prefix}.{segment}" if prefix else segment
            ns_id = _namespace_id(prefix, project_id)
            if ns_id not in emitted:
                emitted.add(ns_id)
                yield GraphNode("Namespace", {"id": ns_id}, {"name": segment})
                yield GraphEdge("HAS_NAMESPACE", parent_label, {"id": parent_id}, "Namespace", {"id": ns_id})
            parent_id, parent_label = ns_id, "Namespace"


# -- Flattening (including nested types) -------------------------------------------


def _arity_suffix(raw_type: RawType) -> str:
    arity = len(raw_type.type_parameters)
    return f"`{arity}" if arity else ""


def _flatten_type(
    raw_type: RawType,
    file_id: str,
    project_id: str,
    parent_type_id: str | None,
    entries: list[_TypeEntry],
    parent_qualified_name: str | None = None,
) -> None:
    slug = _KIND_SLUG[raw_type.kind]
    if parent_type_id is not None:
        type_id = f"{parent_type_id}.{slug}.{raw_type.name.lower()}{_arity_suffix(raw_type)}"
        scope_id, scope_label = parent_type_id, None
        qualified_name = f"{parent_qualified_name}.{raw_type.name}" if parent_qualified_name else raw_type.name
    else:
        ns_id = _namespace_id(raw_type.namespace, project_id)
        type_id = f"{ns_id}.{slug}.{raw_type.name.lower()}{_arity_suffix(raw_type)}"
        scope_id = ns_id
        scope_label = "Namespace" if raw_type.namespace else "Project"
        qualified_name = f"{raw_type.namespace}.{raw_type.name}" if raw_type.namespace else raw_type.name

    entries.append(
        _TypeEntry(
            type_id=type_id,
            kind=raw_type.kind,
            name=raw_type.name,
            namespace=raw_type.namespace,
            raw_type=raw_type,
            file_id=file_id,
            parent_type_id=parent_type_id,
            scope_id=scope_id,
            scope_label=scope_label,
            qualified_name=qualified_name,
        )
    )
    for nested in raw_type.nested_types:
        _flatten_type(nested, file_id, project_id, type_id, entries, qualified_name)


# -- Classification: EXTENDS vs IMPLEMENTS, controller-like, DTO -------------------


def _resolve_target(name: str, type_index: dict[str, list[_TypeEntry]], from_namespace: str) -> _TypeEntry | None:
    matches = type_index.get(name)
    if not matches:
        return None
    same_namespace = [m for m in matches if m.namespace == from_namespace]
    pool = same_namespace or matches
    # Ambiguous (same simple name declared more than once, e.g. in different
    # namespaces) -- pick deterministically rather than guessing; same trade-off as
    # the PL/SQL ingestor's _resolve_call "arbitrary matching overload" precedent.
    return sorted(pool, key=lambda m: m.type_id)[0]


def _classify_base_entry(name: str, type_index: dict[str, list[_TypeEntry]]) -> str:
    matches = type_index.get(name)
    if matches:
        kinds = {m.kind for m in matches}
        if kinds & {"Class", "Record", "Struct"}:
            return "EXTENDS"
        return "IMPLEMENTS"
    # Unresolved -- an external/BCL type (ControllerBase, Exception, IDisposable...)
    # we never ingested ourselves. Fall back to the `I`+uppercase naming convention.
    if len(name) >= 2 and name[0] == "I" and name[1].isupper():
        return "IMPLEMENTS"
    return "EXTENDS"


def _compute_controller_like(entries: list[_TypeEntry], type_index: dict[str, list[_TypeEntry]]) -> set[str]:
    parent_of: dict[str, str] = {}
    for entry in entries:
        if entry.kind == "Interface":
            continue
        for base_name in entry.raw_type.base_list:
            if _classify_base_entry(base_name, type_index) != "EXTENDS":
                continue
            target = _resolve_target(base_name, type_index, entry.namespace)
            if target is not None:
                parent_of[entry.type_id] = target.type_id
                break

    controller_like: set[str] = set()
    for entry in entries:
        has_attribute = any(a.name == "ApiController" for a in entry.raw_type.attributes)
        has_base_name = any(b in _CONTROLLER_BASE_NAMES for b in entry.raw_type.base_list)
        if has_attribute or has_base_name:
            controller_like.add(entry.type_id)

    changed = True
    while changed:
        changed = False
        for entry in entries:
            if entry.type_id in controller_like:
                continue
            if parent_of.get(entry.type_id) in controller_like:
                controller_like.add(entry.type_id)
                changed = True
    return controller_like


def _referenced_type_names(type_text: str | None) -> list[str]:
    if not type_text:
        return []
    return _IDENTIFIER_RE.findall(type_text)


def _http_verb_attribute(attributes: list[RawAttribute]) -> RawAttribute | None:
    return next((a for a in attributes if a.name in _HTTP_VERB_ATTRIBUTES), None)


def _definitive_dto_names(entries: list[_TypeEntry], controller_like: set[str]) -> set[str]:
    names: set[str] = set()
    for entry in entries:
        if entry.type_id not in controller_like:
            continue
        for method in entry.raw_type.methods:
            if _http_verb_attribute(method.attributes) is None:
                continue
            names.update(_referenced_type_names(method.return_type))
            for param in method.parameters:
                names.update(_referenced_type_names(param.type))
    return names


def _final_label(entry: _TypeEntry, definitive_dto_names: set[str], dto_name_suffixes: tuple[str, ...]) -> str:
    if entry.kind not in ("Class", "Record"):
        return entry.kind
    if entry.name in definitive_dto_names:
        return "DTO"
    if any(entry.name.endswith(suffix) for suffix in dto_name_suffixes):
        return "DTO"
    return entry.kind


# -- Emission: type/member nodes + structural edges --------------------------------


def _type_properties(raw_type: RawType, label: str) -> dict[str, object]:
    props: dict[str, object] = {"name": raw_type.name, "accessibility": raw_type.accessibility, "doc": raw_type.doc}
    modifiers = raw_type.modifiers

    if label == "DTO":
        props["is_record"] = raw_type.kind == "Record"
        props["source"] = raw_type.source
        return props

    if raw_type.kind == "Class":
        props.update(
            is_abstract="abstract" in modifiers,
            is_sealed="sealed" in modifiers,
            is_static="static" in modifiers,
            is_partial="partial" in modifiers,
            type_parameters=list(raw_type.type_parameters),
            source=raw_type.source,
        )
    elif raw_type.kind == "Interface":
        props.update(
            is_partial="partial" in modifiers,
            type_parameters=list(raw_type.type_parameters),
            source=raw_type.source,
        )
    elif raw_type.kind == "Struct":
        props.update(
            is_readonly="readonly" in modifiers,
            is_ref="ref" in modifiers,
            is_partial="partial" in modifiers,
            type_parameters=list(raw_type.type_parameters),
            source=raw_type.source,
        )
    elif raw_type.kind == "Enum":
        props.update(underlying_type=raw_type.underlying_type, source=raw_type.source)
    elif raw_type.kind == "Record":
        props.update(
            is_abstract="abstract" in modifiers,
            is_sealed="sealed" in modifiers,
            is_value_type=raw_type.is_value_type,
            positional_parameters=[p.name for p in raw_type.positional_parameters],
            type_parameters=list(raw_type.type_parameters),
            source=raw_type.source,
        )
    elif raw_type.kind == "Delegate":
        props.update(
            return_type=raw_type.return_type,
            type_parameters=list(raw_type.type_parameters),
            source=raw_type.source,
        )
    return props


def _emit_type_and_members(entry: _TypeEntry, final_label_by_type_id: dict[str, str]) -> Iterator[GraphNode | GraphEdge]:
    label = final_label_by_type_id[entry.type_id]
    yield GraphNode(label, {"id": entry.type_id}, _type_properties(entry.raw_type, label))

    if entry.parent_type_id is not None:
        parent_label = final_label_by_type_id[entry.parent_type_id]
        yield GraphEdge("HAS_NESTED_TYPE", parent_label, {"id": entry.parent_type_id}, label, {"id": entry.type_id})
    else:
        yield GraphEdge("HAS_TYPE", entry.scope_label, {"id": entry.scope_id}, label, {"id": entry.type_id})

    yield GraphEdge("DEFINED_IN", label, {"id": entry.type_id}, "File", {"id": entry.file_id})

    for method in entry.raw_type.methods:
        yield from _emit_method(entry.type_id, label, method, entry.qualified_name)
    for prop in entry.raw_type.properties:
        yield from _emit_property(entry.type_id, label, prop)
    for raw_field in entry.raw_type.fields:
        yield from _emit_field(entry.type_id, label, raw_field)
    for event in entry.raw_type.events:
        yield from _emit_event(entry.type_id, label, event)
    for member in entry.raw_type.enum_members:
        yield from _emit_enum_member(entry.type_id, member)
    for i, param in enumerate(entry.raw_type.positional_parameters):
        yield from _emit_parameter(entry.type_id, label, param, i)


def _method_id(type_id: str, method: RawMethod) -> str:
    signature = ",".join((p.type or "").lower() for p in method.parameters)
    return f"{type_id}.method.{method.name.lower()}({signature})"


def _emit_method(
    type_id: str, type_label: str, method: RawMethod, type_qualified_name: str
) -> Iterator[GraphNode | GraphEdge]:
    method_id = _method_id(type_id, method)
    props: dict[str, object] = {
        "name": method.name,
        "kind": method.kind,
        "accessibility": method.accessibility,
        "is_static": "static" in method.modifiers,
        "is_abstract": "abstract" in method.modifiers,
        "is_virtual": "virtual" in method.modifiers,
        "is_override": "override" in method.modifiers,
        "is_async": "async" in method.modifiers,
        "return_type": method.return_type,
        "source": method.source,
        "doc": method.doc,
    }
    # MVC `[ActionName("X")]` renames the routable action -- record it so the view tier can bind a
    # Url.Action/grid transport that targets "X" onto this method (whose C# name differs). Additive.
    action_name = _action_name(method.attributes)
    if action_name:
        props["action_name"] = action_name
    # ADR 0004: an externally-visible method carries a project-independent linkage key (its
    # fully-qualified consumer name), so a placeholder emitted by a *consumer* repo for the same
    # call heals onto this real definition when both share a graph. Private/internal methods can't
    # be a cross-assembly target, so keying them would only risk a spurious same-name merge.
    if _externally_visible(method.accessibility):
        props["linkage_key"] = ids.extern_method_key(f"{type_qualified_name}.{method.name}")
    yield GraphNode("Method", {"id": method_id}, props)
    yield GraphEdge("HAS_METHOD", type_label, {"id": type_id}, "Method", {"id": method_id})
    for i, param in enumerate(method.parameters):
        yield from _emit_parameter(method_id, "Method", param, i)


def _emit_parameter(parent_id: str, parent_label: str, param: RawParameter, position: int) -> Iterator[GraphNode | GraphEdge]:
    param_id = f"{parent_id}.param.{position}.{param.name.lower()}"
    yield GraphNode(
        "Parameter",
        {"id": param_id},
        {
            "name": param.name,
            "type": param.type,
            "direction": param.direction,
            "position": param.position,
            "has_default": param.has_default,
        },
    )
    yield GraphEdge("HAS_PARAMETER", parent_label, {"id": parent_id}, "Parameter", {"id": param_id})


def _emit_property(type_id: str, type_label: str, prop: RawProperty) -> Iterator[GraphNode | GraphEdge]:
    prop_id = f"{type_id}.property.{prop.name.lower()}"
    yield GraphNode(
        "Property",
        {"id": prop_id},
        {
            "name": prop.name,
            "accessibility": prop.accessibility,
            "type": prop.type,
            "has_getter": prop.has_getter,
            "has_setter": prop.has_setter,
            "is_auto_implemented": prop.is_auto_implemented,
            "is_static": prop.is_static,
        },
    )
    yield GraphEdge("HAS_PROPERTY", type_label, {"id": type_id}, "Property", {"id": prop_id})


def _emit_field(type_id: str, type_label: str, raw_field: RawField) -> Iterator[GraphNode | GraphEdge]:
    field_id = f"{type_id}.field.{raw_field.name.lower()}"
    yield GraphNode(
        "Field",
        {"id": field_id},
        {
            "name": raw_field.name,
            "accessibility": raw_field.accessibility,
            "type": raw_field.type,
            "is_readonly": raw_field.is_readonly,
            "is_const": raw_field.is_const,
            "is_static": raw_field.is_static,
        },
    )
    yield GraphEdge("HAS_FIELD", type_label, {"id": type_id}, "Field", {"id": field_id})


def _emit_event(type_id: str, type_label: str, event: RawEvent) -> Iterator[GraphNode | GraphEdge]:
    event_id = f"{type_id}.event.{event.name.lower()}"
    yield GraphNode(
        "Event",
        {"id": event_id},
        {"name": event.name, "accessibility": event.accessibility, "type": event.type, "is_static": event.is_static},
    )
    yield GraphEdge("HAS_EVENT", type_label, {"id": type_id}, "Event", {"id": event_id})


def _emit_enum_member(type_id: str, member: RawEnumMember) -> Iterator[GraphNode | GraphEdge]:
    member_id = f"{type_id}.member.{member.name.lower()}"
    yield GraphNode("EnumMember", {"id": member_id}, {"name": member.name, "value": member.value})
    yield GraphEdge("HAS_ENUM_MEMBER", "Enum", {"id": type_id}, "EnumMember", {"id": member_id})


# -- Emission: cross-references -----------------------------------------------------


def _emit_extends_implements(
    entry: _TypeEntry, type_index: dict[str, list[_TypeEntry]], final_label_by_type_id: dict[str, str]
) -> Iterator[GraphEdge]:
    from_label = final_label_by_type_id[entry.type_id]
    for base_name in entry.raw_type.base_list:
        rel_type = "EXTENDS" if entry.kind == "Interface" else _classify_base_entry(base_name, type_index)
        target = _resolve_target(base_name, type_index, entry.namespace)
        if target is None:
            continue  # external/BCL base -- no node of ours to point to
        to_label = final_label_by_type_id[target.type_id]
        yield GraphEdge(rel_type, from_label, {"id": entry.type_id}, to_label, {"id": target.type_id})


def _edges_for_referenced_names(
    from_label: str,
    from_id: str,
    names: set[str] | list[str],
    from_namespace: str,
    type_index: dict[str, list[_TypeEntry]],
    final_label_by_type_id: dict[str, str],
) -> Iterator[GraphEdge]:
    for name in names:
        target = _resolve_target(name, type_index, from_namespace)
        if target is None or target.type_id == from_id:
            continue
        to_label = final_label_by_type_id[target.type_id]
        yield GraphEdge("DEPENDS_ON", from_label, {"id": from_id}, to_label, {"id": target.type_id})


def _emit_type_references(
    entry: _TypeEntry, type_index: dict[str, list[_TypeEntry]], final_label_by_type_id: dict[str, str]
) -> Iterator[GraphEdge]:
    for method in entry.raw_type.methods:
        method_id = _method_id(entry.type_id, method)
        names = set(_referenced_type_names(method.return_type))
        for param in method.parameters:
            names.update(_referenced_type_names(param.type))
        yield from _edges_for_referenced_names("Method", method_id, names, entry.namespace, type_index, final_label_by_type_id)

    for prop in entry.raw_type.properties:
        prop_id = f"{entry.type_id}.property.{prop.name.lower()}"
        names = _referenced_type_names(prop.type)
        yield from _edges_for_referenced_names("Property", prop_id, names, entry.namespace, type_index, final_label_by_type_id)

    for raw_field in entry.raw_type.fields:
        field_id = f"{entry.type_id}.field.{raw_field.name.lower()}"
        names = _referenced_type_names(raw_field.type)
        yield from _edges_for_referenced_names("Field", field_id, names, entry.namespace, type_index, final_label_by_type_id)


def _pick(candidates: list[tuple[str, str, str]]) -> tuple[str, str]:
    chosen = sorted(candidates, key=lambda c: c[1])[0]
    return chosen[0], chosen[1]


def _resolve_call(
    call: RawCall,
    from_type_id: str,
    from_namespace: str,
    method_index: dict[str, list[tuple[str, str, str]]],
    type_index: dict[str, list[_TypeEntry]],
    member_types: dict[str, str],
) -> tuple[str, str] | None:
    """Best-effort call resolution -- no real symbol table, so this is name-based
    with a few tiers, cheapest/most-precise first:

    1. A receiver naming one of the caller's own fields/properties (the common `_x.Y()`
       DI pattern): resolve the receiver's declared type, then require the match to
       be a method of *that* type specifically. This exists so a call like
       `_service.GetById()` inside a method that happens to itself be named
       `GetById` doesn't spuriously resolve to itself -- a same-type-name coincidence
       is otherwise indistinguishable from real recursion once we've thrown away the
       receiver (see RawCall's docstring).
    2. A bare call or `this.Y()`: same type as the caller, then same namespace, then
       anywhere else in the codebase.
    3. A receiver we can't resolve at all (a local variable, an external/BCL type,
       etc.): same namespace, then anywhere -- deliberately skipping the same-type
       tier, since a qualified call on an unresolved receiver is unlikely to
       coincidentally be a same-type call.

    Ambiguous matches within a tier pick deterministically rather than guessing --
    same trade-off as the PL/SQL ingestor's _resolve_call."""
    candidates = method_index.get(call.name)
    if not candidates:
        return None

    if call.receiver is not None and call.receiver != "this":
        receiver_type = member_types.get(call.receiver)
        if receiver_type is not None:
            for type_name in _referenced_type_names(receiver_type):
                target_type = _resolve_target(type_name, type_index, from_namespace)
                if target_type is None:
                    continue
                matches = [c for c in candidates if c[0] == target_type.type_id]
                if matches:
                    return _pick(matches)
            return None  # resolved the receiver's type but it has no matching method
        same_namespace = [c for c in candidates if c[2] == from_namespace]
        return _pick(same_namespace or candidates)

    same_type = [c for c in candidates if c[0] == from_type_id]
    if same_type:
        return _pick(same_type)
    same_namespace = [c for c in candidates if c[2] == from_namespace]
    return _pick(same_namespace or candidates)


_EXTERNALLY_VISIBLE = frozenset({"public", "protected", "protected internal"})


def _externally_visible(accessibility: str) -> bool:
    """Whether a member is reachable from another assembly/repo (so worth a cross-repo linkage
    key). `internal`/`private`/`private protected` are assembly- or type-scoped and never are."""
    return accessibility in _EXTERNALLY_VISIBLE


def _type_qualified_name(type_text: str) -> str:
    """The dotted type name inside a declared-type string, generics/arrays/nullable stripped:
    `List<Foo>` -> `List`, `System.Text.StringBuilder` -> `System.Text.StringBuilder`,
    `IWidgetService` -> `IWidgetService`. Keeps dots (they may carry the namespace) so the
    resulting linkage key is as qualified as the source was."""
    text = type_text.split("<", 1)[0]
    return text.replace("[]", "").replace("?", "").replace("*", "").strip()


def _csharp_origin(qualified_type: str) -> str:
    """Coarse provenance for an unresolved callee (ADR 0004): `stdlib` for the BCL
    (System.*/Microsoft.*), else `external` -- the candidate a sibling-repo ingest could heal."""
    root = qualified_type.split(".", 1)[0]
    return "stdlib" if root in ("System", "Microsoft") else "external"


def _external_callee(
    call: RawCall,
    from_namespace: str,
    type_index: dict[str, list[_TypeEntry]],
    member_types: dict[str, str],
) -> tuple[str, str, str] | None:
    """For a call that didn't resolve to a project method, decide whether it names an *external*
    callee whose receiver type we can name -- returning (qualified_name, short_name, origin) for a
    placeholder -- or None to skip.

    Skips (no placeholder) when:
    * the receiver is unnameable (a bare/`this` call, or a receiver too complex for RawCall to
      name) -- an unresolved bare call is a dangling *project* reference, not an external one; or
    * the receiver's type IS a project type -- then a missing member is intra-project dangling,
      left dropped exactly as before (the ADR guard)."""
    if call.receiver is None or call.receiver == "this":
        return None
    receiver_type_text = member_types.get(call.receiver)
    if receiver_type_text is not None:
        receiver_type = _type_qualified_name(receiver_type_text)  # `_x.Y()` -> _x's declared type
    else:
        receiver_type = call.receiver  # a static call on a type name, e.g. `Console.WriteLine()`
    if not receiver_type:
        return None
    simple = receiver_type.rsplit(".", 1)[-1]
    if _resolve_target(simple, type_index, from_namespace) is not None:
        return None  # a project type is missing this member -> intra-project dangling, not external
    qualified = f"{receiver_type}.{call.name}"
    return qualified, call.name, _csharp_origin(receiver_type)


def _external_method_node(eid: str, short_name: str, qualified_name: str, origin: str) -> GraphNode:
    """A placeholder for an unresolved call, keyed by its cross-repo linkage so a later ingest of
    the defining repo folds it in (ADR 0004). Shape mirrors a real Method so the same CALLS
    queries reach it; `resolved:false` + `origin` mark it."""
    return GraphNode("Method", {"id": eid}, {
        "name": short_name,
        "qualified_name": qualified_name,
        "resolved": False,
        "origin": origin,
        "linkage_key": eid,
    })


def _emit_calls(
    entry: _TypeEntry,
    method_index: dict[str, list[tuple[str, str, str]]],
    type_index: dict[str, list[_TypeEntry]],
    emitted_externs: set[str],
) -> Iterator[GraphNode | GraphEdge]:
    member_types = {f.name: f.type for f in entry.raw_type.fields if f.type}
    member_types.update({p.name: p.type for p in entry.raw_type.properties if p.type})

    for method in entry.raw_type.methods:
        method_id = _method_id(entry.type_id, method)
        for call in method.calls:
            if call.http_url:  # an HttpClient call -> the route seam, not a CALLS to GetAsync
                yield from _emit_http_call(method_id, call, emitted_externs)
                continue
            target = _resolve_call(call, entry.type_id, entry.namespace, method_index, type_index, member_types)
            if target is not None:
                _, target_method_id = target
                # `resolution` is the shared provenance key on CALLS across ingestors (Python/C use
                # it too). The tree-sitter engine is name-based, so "name" -- the Roslyn deep pass
                # re-emits the same edge with a stronger value (e.g. "di-resolved").
                yield GraphEdge("CALLS", "Method", {"id": method_id}, "Method", {"id": target_method_id}, {"resolution": "name"})
                continue
            # Unresolved (ADR 0004): if we can name the callee's receiver type and it's NOT a
            # project type (a BCL call like Console.WriteLine, or a method from a repo not in this
            # ingest), surface it as a placeholder Method keyed by its cross-repo linkage, reached
            # by an ordinary CALLS edge. A later ingest of the defining repo heals it in place.
            external = _external_callee(call, entry.namespace, type_index, member_types)
            if external is None:
                continue  # unnameable receiver, or a missing member of a *project* type (dangling)
            qualified, short_name, origin = external
            eid = ids.extern_method_key(qualified)
            if eid not in emitted_externs:
                emitted_externs.add(eid)
                yield _external_method_node(eid, short_name, qualified, origin)
            yield GraphEdge("CALLS", "Method", {"id": method_id}, "Method", {"id": eid}, {"resolution": "external"})


def _emit_http_call(method_id: str, call: RawCall, emitted_externs: set[str]) -> Iterator[GraphNode | GraphEdge]:
    """ADR 0006: materialize an HttpClient call as `Method -CALLS_API-> APIEndpoint{resolved:false}`,
    a route-keyed placeholder (shared core key, so C# and TS callers converge). The stitch hook
    resolves it to the handler across repos."""
    route = http_seam.normalize_route(call.http_url or "")
    if not route:
        return
    verb = (call.http_method or "ANY").upper()
    endpoint_id = http_seam.endpoint_placeholder_key(verb, route)
    if endpoint_id not in emitted_externs:
        emitted_externs.add(endpoint_id)
        yield GraphNode("APIEndpoint", {"id": endpoint_id},
                        {"name": f"{verb} {route}", "route": route, "http_method": verb,
                         "resolved": False, "provenance": "http-derived"})
    yield GraphEdge("CALLS_API", "Method", {"id": method_id}, "APIEndpoint", {"id": endpoint_id},
                    {"resolution": "name", "provenance": "http-derived"})


def _route_template(attributes: list[RawAttribute]) -> str | None:
    for attribute in attributes:
        if attribute.name == "Route" and attribute.arguments:
            return attribute.arguments[0]
    return None


def _action_name(attributes: list[RawAttribute]) -> str | None:
    """The `[ActionName("X")]` override that renames the routable action -- the name the MVC
    routing (and thus a view's Url.Action / grid transport) targets, distinct from the C#
    method name. None when absent (the action name is then the method name)."""
    for attribute in attributes:
        if attribute.name in ("ActionName", "ActionNameAttribute") and attribute.arguments:
            return attribute.arguments[0].strip("\"'")
    return None


def _endpoint_route(class_route: str | None, method_route: str | None, controller_name: str) -> str | None:
    parts = [p.strip("/") for p in (class_route, method_route) if p]
    if not parts:
        return None
    controller_token = controller_name[: -len("Controller")] if controller_name.endswith("Controller") else controller_name
    route = "/".join(parts)
    return route.replace("[controller]", controller_token).replace("[Controller]", controller_token)


def _emit_endpoints(
    entry: _TypeEntry,
    controller_like: set[str],
    type_index: dict[str, list[_TypeEntry]],
    final_label_by_type_id: dict[str, str],
) -> Iterator[GraphNode | GraphEdge]:
    if entry.type_id not in controller_like:
        return
    class_route = _route_template(entry.raw_type.attributes)

    for method in entry.raw_type.methods:
        http_attribute = _http_verb_attribute(method.attributes)
        if http_attribute is None:
            continue
        method_route = http_attribute.arguments[0] if http_attribute.arguments else None
        route = _endpoint_route(class_route, method_route, entry.name)
        if route is None:
            # Convention-routed action (no attribute route, e.g. classic MVC `[HttpPost] Save`): its
            # URL is `/Controller/Action`, which we can't form here without guessing. The stitch hook
            # resolves a caller to it by that convention and links the caller's own route-keyed node,
            # so emitting a routeless endpoint here would only DUPLICATE that node. Leave it to the seam.
            continue
        verb = _HTTP_VERB_ATTRIBUTES[http_attribute.name]
        route = http_seam.normalize_route(route)
        method_id = _method_id(entry.type_id, method)
        # Route-keyed id (shared core key), NOT `{method_id}.endpoint`: a caller reaching this same
        # route+verb materializes the identical id, so declaration and call converge on ONE node
        # instead of two (ADR 0006).
        endpoint_id = http_seam.endpoint_placeholder_key(verb, route)

        yield GraphNode(
            "APIEndpoint",
            {"id": endpoint_id},
            {"route": route, "http_method": verb, "name": method.name},
        )
        yield GraphEdge("EXPOSES_ENDPOINT", "Method", {"id": method_id}, "APIEndpoint", {"id": endpoint_id})

        for param in method.parameters:
            for name in _referenced_type_names(param.type):
                target = _resolve_target(name, type_index, entry.namespace)
                if target is not None and final_label_by_type_id.get(target.type_id) == "DTO":
                    yield GraphEdge("ACCEPTS", "APIEndpoint", {"id": endpoint_id}, "DTO", {"id": target.type_id})

        for name in _referenced_type_names(method.return_type):
            target = _resolve_target(name, type_index, entry.namespace)
            if target is not None and final_label_by_type_id.get(target.type_id) == "DTO":
                yield GraphEdge("RETURNS", "APIEndpoint", {"id": endpoint_id}, "DTO", {"id": target.type_id})
