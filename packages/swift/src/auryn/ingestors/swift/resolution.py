"""Project-wide pass (phase 2): turn the per-file RawFile list into graph nodes/edges, applying
the cross-file decisions a single file can't -- re-attaching each `extension` block's members to
the type they belong to, splitting a type's inheritance clause into EXTENDS (superclass /
inherited protocol) vs IMPLEMENTS (conformance) by the resolved target's kind, resolving
parameter/return/property type references to project types, and best-effort call resolution.

Swift blends the Rust and Java templates (see schema.py):

* like Rust, a type's members can live in a separate `extension` block (possibly in another
  file), so methods/properties are indexed by their resolved owning type; and Swift has free
  top-level `Function`s alongside type-bound `Method`s.
* like Java, the type hierarchy is nominal -- but Swift writes superclass and conformances in one
  comma list, so the EXTENDS/IMPLEMENTS split is decided here from the resolved target's kind
  (a project Class in a class's list -> EXTENDS; a project Interface -> IMPLEMENTS; any name in a
  protocol's list -> EXTENDS).

Name-based only -- this is the fast tree-sitter engine, so every resolved CALLS edge is stamped
`resolution="name"`. A future deep SourceKit-LSP pass would re-emit the same node ids with a
stronger provenance (the two-phase model). Ambiguous or unresolved-but-in-project references
produce no edge rather than a wrong guess; an unresolved-but-nameable out-of-project callee/import
becomes a first-class placeholder reached by an ordinary edge (ADR 0004) -- the same trade-offs as
the Rust/Java ingestors."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field

from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.ingestors.swift import ids, swiftpm
from auryn.ingestors.swift.raw_model import RawFunction, RawProperty, RawType

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Tokens that appear in type text but never name a project type (Swift type keywords/sugar). A
# name that isn't a project type simply won't be in the type_index, so this is only a fast skip.
_NON_TYPE_TOKENS = frozenset(
    {"Void", "Any", "AnyObject", "Self", "self", "inout", "some", "any", "throws", "async", "rethrows"}
)
# Apple/system frameworks -- an import of one is a terminal stdlib dependency (origin="stdlib"),
# never upgraded by a future ingest; anything else is origin="external" (a sibling package a later
# ingest could define). Not exhaustive -- the surface seen in practice.
_SYSTEM_MODULES = frozenset(
    {
        "Foundation", "UIKit", "SwiftUI", "Combine", "CoreData", "CoreGraphics", "Dispatch",
        "os", "Darwin", "Swift", "XCTest", "AppKit", "CoreLocation", "MapKit", "AVFoundation",
        "CoreImage", "QuartzCore", "WebKit", "Network", "CryptoKit", "SceneKit", "SpriteKit",
        "Metal", "CloudKit", "StoreKit", "GameKit", "HealthKit", "ARKit", "CoreML", "Vision",
        "CoreBluetooth", "Security", "SystemConfiguration", "Accelerate", "simd",
    }
)
# Common Swift standard-library value types -- a method/static call on one is origin="stdlib".
_STDLIB_TYPES = frozenset(
    {
        "String", "Substring", "Int", "Int8", "Int16", "Int32", "Int64", "UInt", "Double",
        "Float", "Bool", "Character", "Array", "Dictionary", "Set", "Optional", "Result",
        "Range", "ClosedRange", "URL", "Data", "Date", "UUID", "Decimal",
    }
)


@dataclass
class _Entry:
    type_id: str
    kind: str  # "Class" | "Struct" | "Enum" | "Interface"
    name: str
    raw: RawType
    file_id: str
    parent_type_id: str | None
    scope_id: str  # the owning MODULE id (top-level) or the parent type_id (nested)
    scope_label: str | None  # "Module" (top-level) ; None (nested, edged via HAS_NESTED_TYPE)
    module_id: str = ""  # the DECLARING file's module id -- used to resolve a bare type ref to the
    #   same target first (an unqualified name can't reach another module); set for every entry.


@dataclass
class _Callable:
    id: str
    label: str  # "Function" | "Method"
    scope_id: str  # the owning MODULE id for a Function; the owning type_id for a Method
    owner_id: str | None  # owning type id for a Method; None for a free function
    owner_label: str | None  # owning type kind for HAS_METHOD; None for a free function
    raw: RawFunction
    decl_module: str = ""  # the module of the file this callable is DECLARED in (for an extension
    #   member, the extension's module, NOT the owner type's) -- the scope a bare ref resolves in.


@dataclass
class _External:
    """ADR 0004: a nameable but unresolved callee -- a placeholder keyed by its cross-repo
    linkage. `eid` is `extern://swift/<qualified>` (the placeholder node id AND its linkage_key),
    so a later ingest of the defining repo heals the call onto the real definition."""

    eid: str
    label: str  # "Function" | "Method"
    name: str
    qualified: str
    origin: str  # "stdlib" | "external"
    extra: dict = field(default_factory=dict)  # extra CALLS-edge props (e.g. receiver_type)


def resolve(raw_files, project_id: str) -> Iterator[GraphNode | GraphEdge]:
    project_name = project_id.split("://", 1)[-1]

    # -- files + modules (SwiftPM targets) + imports ---------------------------------------
    # Each file's MODULE (target) is path-derived (swiftpm.module_for_path) and is the scope root for
    # its top-level types/functions, so two targets that both declare `Foo` stay distinct.
    file_ids: dict[str, str] = {}
    module_id_of: dict[str, str] = {}   # rf.path -> owning module id
    modules: dict[str, str] = {}        # module id -> module (target) name
    emitted_modules: set[str] = set()
    for rf in raw_files:
        fid = ids.file_id(project_id, rf.path)
        file_ids[rf.path] = fid
        yield GraphNode("File", {"id": fid}, {"name": _basename(rf.path), "path": _norm(rf.path)})
        yield GraphEdge("HAS_FILE", "Project", {"id": project_id}, "File", {"id": fid})
        mname = swiftpm.module_for_path(rf.path, project_name)
        mid = ids.module_id(project_id, mname)
        module_id_of[rf.path] = mid
        modules.setdefault(mid, mname)
        yield from _emit_imports(rf, fid, emitted_modules)
    # Module nodes (one per SwiftPM target). Each carries a `linkage_key` so an `import <Target>` --
    # from a sibling target or another repo -- heals onto it (ADR 0004, reconcile_linkages).
    for mid, mname in sorted(modules.items()):
        yield GraphNode("Module", {"id": mid}, {
            "name": mname, "qualified_name": mname, "linkage_key": ids.extern_module_key(mname),
        })
        yield GraphEdge("HAS_MODULE", "Project", {"id": project_id}, "Module", {"id": mid})

    # -- collect type entries (top-level scoped under the file's MODULE; flatten nested) ----
    entries: list[_Entry] = []
    for rf in raw_files:
        for rt in rf.types:
            _flatten(rt, file_ids[rf.path], module_id_of[rf.path], None, "Module", entries)

    type_index: dict[str, list[_Entry]] = defaultdict(list)
    for e in entries:
        type_index[e.name].append(e)

    # -- attach extensions to their resolved owning type -----------------------------------
    ext_attachments: list[tuple[_Entry, object, str]] = []  # (owner_entry, RawExtension, ext_module)
    for rf in raw_files:
        ext_module = module_id_of[rf.path]
        for ext in rf.extensions:
            # `extension Foo` resolves to the Foo visible in THIS file's module first.
            owner = _resolve_target(ext.type_name, type_index, ext_module)
            if owner is not None:  # an extension of a non-project type has no landing node
                ext_attachments.append((owner, ext, ext_module))

    # -- callables (Method: own + extension members + init ; Function: top-level) ----------
    callables: list[_Callable] = []
    seen_callables: set[str] = set()
    method_index: dict[str, list[tuple[str, str, str]]] = defaultdict(list)  # name -> (owner_id, method_id, owner_name)
    function_index: dict[str, list[tuple[str, str]]] = defaultdict(list)  # name -> (func_id, scope_id)

    def _add_method(owner: _Entry, m: RawFunction, decl_module: str) -> None:
        mid = ids.method_id(owner.type_id, m.name, [p.type for p in m.parameters])
        if mid in seen_callables:
            return
        seen_callables.add(mid)
        callables.append(_Callable(mid, "Method", owner.type_id, owner.type_id, owner.kind, m, decl_module=decl_module))
        method_index[m.name].append((owner.type_id, mid, owner.name))

    for e in entries:
        for m in e.raw.methods:
            _add_method(e, m, e.module_id)
    for owner, ext, ext_module in ext_attachments:
        for m in ext.methods:  # an extension member is DECLARED in the extension's module, not the owner's
            _add_method(owner, m, ext_module)
    for rf in raw_files:
        mid = module_id_of[rf.path]
        for fn in rf.functions:
            fid = ids.function_id(mid, fn.name, [p.type for p in fn.parameters])
            if fid in seen_callables:
                continue
            seen_callables.add(fid)
            callables.append(_Callable(fid, "Function", mid, None, None, fn, decl_module=mid))
            function_index[fn.name].append((fid, mid))

    # -- properties (own + extension), and the per-type member->type map for receiver typing --
    properties: list[tuple[str, str, RawProperty, str]] = []  # (owner_id, owner_label, prop, module)
    seen_props: set[str] = set()
    type_members: dict[str, dict[str, str]] = defaultdict(dict)  # type_id -> {member name -> type text}

    def _add_property(owner_id: str, owner_label: str, p: RawProperty, module: str) -> None:
        pid = ids.property_id(owner_id, p.name)
        if pid not in seen_props:
            seen_props.add(pid)
            properties.append((owner_id, owner_label, p, module))
        if p.type:
            type_members[owner_id].setdefault(p.name, p.type)

    for e in entries:
        for p in e.raw.properties:
            _add_property(e.type_id, e.kind, p, e.module_id)
    for owner, ext, ext_module in ext_attachments:
        for p in ext.properties:
            _add_property(owner.type_id, owner.kind, p, ext_module)

    # -- pass 1: nodes + structural edges (so pass 2's cross-refs have landing nodes) -------
    type_by_id = {e.type_id: e for e in entries}
    for e in entries:
        yield from _emit_type(e)
    for owner_id, owner_label, p, _m in properties:
        yield from _emit_property(owner_id, owner_label, p)
    for cal in callables:
        yield from _emit_callable_node(cal, _real_linkage(cal, project_name, type_by_id))

    # -- pass 2: hierarchy, type references, calls -----------------------------------------
    for e in entries:
        yield from _emit_hierarchy(e, type_index)
    for owner, ext, ext_module in ext_attachments:  # `extension Foo: Bar` adds a conformance to Foo
        for name in ext.conformances:
            target = _resolve_target(name, type_index, ext_module)
            if target is not None and target.kind == "Interface" and target.type_id != owner.type_id:
                yield GraphEdge("IMPLEMENTS", owner.kind, {"id": owner.type_id}, "Interface", {"id": target.type_id})

    for owner_id, _owner_label, p, module in properties:
        pid = ids.property_id(owner_id, p.name)
        yield from _depends_on("Property", pid, [p.type], type_index, owner_id, module)

    emitted_externs: set[str] = set()
    for cal in callables:
        yield from _emit_calls(cal, type_members, type_index, function_index, method_index, emitted_externs)
        type_texts = [cal.raw.return_type] + [p.type for p in cal.raw.parameters]
        yield from _depends_on(cal.label, cal.id, type_texts, type_index, cal.owner_id, cal.decl_module)


# -- collection ----------------------------------------------------------------------------

def _flatten(rt: RawType, file_id: str, root_id: str, parent_type_id: str | None, scope_label: str | None, entries: list[_Entry]) -> None:
    # `root_id` is the owning MODULE id for a top-level type (scope_label "Module"); a nested type
    # scopes under its parent type instead.
    if parent_type_id is not None:
        tid = ids.type_id(parent_type_id, rt.kind, rt.name, rt.type_parameters)
        scope_id, label = parent_type_id, None
    else:
        scope_id, label = root_id, scope_label
        tid = ids.type_id(scope_id, rt.kind, rt.name, rt.type_parameters)
    # `root_id` is the file's module id throughout the recursion (it is the top-level scope and is
    # threaded unchanged into nested types), so it is the declaring module of every entry here.
    entries.append(_Entry(tid, rt.kind, rt.name, rt, file_id, parent_type_id, scope_id, label, module_id=root_id))
    for nested in rt.nested_types:
        _flatten(nested, file_id, root_id, tid, None, entries)


# -- pass 1 emitters -----------------------------------------------------------------------

_KIND_BY_SLUG = {"class": "Class", "struct": "Struct", "enum": "Enum", "protocol": "Interface"}


def _parent_kind(parent_type_id: str) -> str:
    tail = parent_type_id.rsplit(".", 2)[-2] if parent_type_id else "class"
    return _KIND_BY_SLUG.get(tail.split("`")[0], "Class")


def _emit_type(e: _Entry) -> Iterator[GraphNode | GraphEdge]:
    rt = e.raw
    props = {
        "name": rt.name,
        "accessibility": rt.accessibility,
        "modifiers": list(rt.modifiers),
        "attributes": list(rt.attributes),
        "type_parameters": list(rt.type_parameters),
        "is_final": "final" in rt.modifiers,
        "source": rt.source,
        "doc": rt.doc,
        "line_start": rt.line_start,
        "line_end": rt.line_end,
    }
    yield GraphNode(e.kind, {"id": e.type_id}, props)
    if e.parent_type_id is not None:
        yield GraphEdge("HAS_NESTED_TYPE", _parent_kind(e.parent_type_id), {"id": e.parent_type_id}, e.kind, {"id": e.type_id})
    else:
        yield GraphEdge("HAS_TYPE", e.scope_label, {"id": e.scope_id}, e.kind, {"id": e.type_id})
    yield GraphEdge("DEFINED_IN", e.kind, {"id": e.type_id}, "File", {"id": e.file_id})

    if e.kind == "Enum":
        for member in rt.enum_members:
            mid = ids.enum_member_id(e.type_id, member.name)
            yield GraphNode("EnumMember", {"id": mid}, {
                "name": member.name,
                "associated_values": list(member.associated_values),
                "raw_value": member.raw_value,
            })
            yield GraphEdge("HAS_ENUM_MEMBER", "Enum", {"id": e.type_id}, "EnumMember", {"id": mid})


def _emit_property(owner_id: str, owner_label: str, p: RawProperty) -> Iterator[GraphNode | GraphEdge]:
    pid = ids.property_id(owner_id, p.name)
    yield GraphNode("Property", {"id": pid}, {
        "name": p.name,
        "type": p.type,
        "accessibility": p.accessibility,
        "is_static": p.is_static,
        "is_computed": p.is_computed,
        "is_constant": p.is_constant,
        "line_start": p.line_start,
        "line_end": p.line_end,
    })
    yield GraphEdge("HAS_PROPERTY", owner_label, {"id": owner_id}, "Property", {"id": pid})


def _emit_callable_node(cal: _Callable, linkage_key: str | None) -> Iterator[GraphNode | GraphEdge]:
    fn = cal.raw
    props = {
        "name": fn.name,
        "kind": fn.kind,
        "accessibility": fn.accessibility,
        "modifiers": list(fn.modifiers),
        "attributes": list(fn.attributes),
        "is_static": fn.is_static,
        "is_async": "async" in fn.modifiers,
        "has_body": fn.has_body,
        "return_type": fn.return_type,
        "source": fn.source,
        "doc": fn.doc,
        "line_start": fn.line_start,
        "line_end": fn.line_end,
    }
    if linkage_key is not None:  # ADR 0004: a public/open callable is externally visible
        props["linkage_key"] = linkage_key
    if cal.label == "Method":
        yield GraphNode("Method", {"id": cal.id}, props)
        yield GraphEdge("HAS_METHOD", cal.owner_label, {"id": cal.owner_id}, "Method", {"id": cal.id})
    else:
        yield GraphNode("Function", {"id": cal.id}, props)
        yield GraphEdge("HAS_FUNCTION", "Module", {"id": cal.scope_id}, "Function", {"id": cal.id})
    for i, p in enumerate(fn.parameters):
        pid = ids.parameter_id(cal.id, i, p.name)
        yield GraphNode("Parameter", {"id": pid}, {
            "name": p.name, "type": p.type, "position": p.position,
            "label": p.label, "is_variadic": p.is_variadic,
        })
        yield GraphEdge("HAS_PARAMETER", cal.label, {"id": cal.id}, "Parameter", {"id": pid})


def _real_linkage(cal: _Callable, project_name: str, type_by_id: dict[str, _Entry]) -> str | None:
    """ADR 0004 cross-repo `linkage_key` for a real callable, or None for one not externally
    visible (only `public`/`open` items cross a module boundary). Name-based, in the form a
    consumer writes: a method -> `Type.name`; a free function -> `<module>.name`, where `<module>`
    is its owning SwiftPM target (not the project), so a cross-repo consumer of that target heals."""
    fn = cal.raw
    if fn.accessibility not in ("public", "open"):
        return None
    if cal.label == "Method":
        owner = type_by_id.get(cal.owner_id)
        if owner is None:
            return None
        return ids.extern_function_key(f"{owner.name}.{fn.name}")
    module_name = cal.scope_id.rsplit("/module/", 1)[-1] if "/module/" in cal.scope_id else project_name
    return ids.extern_function_key(f"{module_name}.{fn.name}")


# -- pass 2: hierarchy, type references, calls ---------------------------------------------

def _resolve_target(name: str | None, type_index: dict[str, list[_Entry]],
                    from_module: str | None = None) -> _Entry | None:
    """Resolve a (bare) type name to a project type. An unqualified Swift type name resolves within
    the referring file's module first, so prefer a candidate declared in `from_module`; only fall
    back to a cross-module match (deterministic by id) when the module has no such type -- otherwise
    `Foo` in two targets would bind to whichever sorts first, regardless of who refers to it."""
    if not name:
        return None
    matches = type_index.get(name)
    if not matches:
        return None
    if from_module is not None:
        local = [m for m in matches if m.module_id == from_module]
        if local:
            return sorted(local, key=lambda m: m.type_id)[0]
    return sorted(matches, key=lambda m: m.type_id)[0]  # deterministic on cross-module ambiguity


def _method_on_type(type_id: str, name: str, method_index) -> str | None:
    matches = [c for c in method_index.get(name, []) if c[0] == type_id]
    return sorted(matches, key=lambda c: c[1])[0][1] if matches else None


def _referenced_names(type_text: str | None) -> list[str]:
    if not type_text:
        return []
    return [n for n in _IDENTIFIER_RE.findall(type_text) if n not in _NON_TYPE_TOKENS]


def _emit_hierarchy(e: _Entry, type_index: dict[str, list[_Entry]]) -> Iterator[GraphEdge]:
    """Split the inheritance clause: a protocol inherits protocols (EXTENDS); a class/struct/enum
    conforms to a protocol (IMPLEMENTS) or, for a class, subclasses a class (EXTENDS). Decided by
    the resolved target's kind -- only project types produce an edge (name-based, ADR 0004)."""
    for name in e.raw.inherits:
        target = _resolve_target(name, type_index, e.module_id)
        if target is None or target.type_id == e.type_id:
            continue
        if e.kind == "Interface":
            rel = "EXTENDS"  # a protocol's inherited protocols
        elif target.kind == "Interface":
            rel = "IMPLEMENTS"  # a class/struct/enum conforming to a protocol
        elif target.kind == "Class" and e.kind == "Class":
            rel = "EXTENDS"  # a class superclass
        else:
            continue  # e.g. an enum's raw-value type -- not a hierarchy edge
        yield GraphEdge(rel, e.kind, {"id": e.type_id}, target.kind, {"id": target.type_id})


def _resolve_call(call, cal: _Callable, member_types: dict[str, str], type_index, function_index, method_index):
    """A resolved local target `(target_id, target_label, extra_props)`, an `_External` placeholder
    for a nameable out-of-project callee (ADR 0004), or None to drop (unnameable, or an
    intra-project dangling reference a resync should clean up rather than materialize)."""
    recv = call.receiver

    if recv is None:  # bare `foo()`, implicit-self method, or construction `Foo(...)`
        if cal.owner_id:  # inside a method, an unqualified call resolves to an own member first
            mid = _method_on_type(cal.owner_id, call.name, method_index)
            if mid:
                return mid, "Method", {}
        cands = function_index.get(call.name)
        if cands:
            # Prefer a free function in the CALLER's DECLARING module (an unqualified call can't reach
            # another module). `cal.decl_module` is the file the callable is written in -- for an
            # extension member, the extension's module, not the owner type's.
            same = [c for c in cands if c[1] == cal.decl_module]
            return sorted(same or cands)[0][0], "Function", {}
        tgt = _resolve_target(call.name, type_index, cal.decl_module)  # a project type -> its initializer
        if tgt is not None:
            init_id = _method_on_type(tgt.type_id, "init", method_index)
            if init_id:
                return init_id, "Method", {"receiver_type": tgt.name}
            return None  # a project type's implicit/memberwise init -> intra-project, drop
        if call.name[:1].isupper():  # construction of a non-project type -> external initializer
            qualified = f"{call.name}.init"
            origin = "stdlib" if call.name in _STDLIB_TYPES else "external"
            return _External(ids.extern_function_key(qualified), "Method", "init", qualified, origin,
                             {"receiver_type": call.name})
        return None  # a bare free call to a non-project function -- can't name it -> drop

    if recv == "self":
        if cal.owner_id:
            mid = _method_on_type(cal.owner_id, call.name, method_index)
            if mid:
                return mid, "Method", {}
        return None  # a missing method on our own type -> intra-project dangling -> drop

    rtype = member_types.get(recv)  # receiver is a known property/param -> its declared type owns it
    if rtype is not None:
        names = _referenced_names(rtype)
        for tn in names:
            tgt = _resolve_target(tn, type_index, cal.decl_module)
            if tgt is not None:
                mid = _method_on_type(tgt.type_id, call.name, method_index)
                if mid:
                    return mid, "Method", {"receiver_type": tgt.name}
        head = names[0] if names else None
        if head is None or _resolve_target(head, type_index, cal.decl_module) is not None:
            return None  # receiver's project type lacks the member -> intra-project dangling, drop
        qualified = f"{head}.{call.name}"  # receiver is an external type -> a nameable method
        origin = "stdlib" if head in _STDLIB_TYPES else "external"
        return _External(ids.extern_function_key(qualified), "Method", call.name, qualified, origin,
                         {"receiver_type": head})

    tgt = _resolve_target(recv, type_index, cal.decl_module)  # a static/type call `Type.method()`
    if tgt is not None:
        mid = _method_on_type(tgt.type_id, call.name, method_index)
        if mid:
            return mid, "Method", {"receiver_type": tgt.name}
        return None  # static call on a project type missing the member -> intra-project, drop
    if recv[:1].isupper():  # static/member call on an external type
        qualified = f"{recv}.{call.name}"
        origin = "stdlib" if recv in _STDLIB_TYPES else "external"
        return _External(ids.extern_function_key(qualified), "Method", call.name, qualified, origin,
                         {"receiver_type": recv})
    cands = method_index.get(call.name)  # lowercase receiver of unknown type -> name-based pick
    if cands:
        return sorted(cands, key=lambda x: x[1])[0][1], "Method", {}
    return None


def _emit_calls(cal: _Callable, type_members, type_index, function_index, method_index,
                emitted_externs: set[str]) -> Iterator[GraphNode | GraphEdge]:
    member_types: dict[str, str] = {}
    if cal.owner_id:
        member_types.update(type_members.get(cal.owner_id, {}))
    member_types.update({p.name: p.type for p in cal.raw.parameters if p.type})

    emitted: set[str] = set()
    for call in cal.raw.calls:
        hit = _resolve_call(call, cal, member_types, type_index, function_index, method_index)
        if hit is None:
            continue
        if isinstance(hit, _External):
            if hit.eid in emitted:
                continue
            emitted.add(hit.eid)
            if hit.eid not in emitted_externs:
                emitted_externs.add(hit.eid)
                yield GraphNode(hit.label, {"id": hit.eid}, {
                    "name": hit.name, "qualified_name": hit.qualified, "resolved": False,
                    "origin": hit.origin, "linkage_key": hit.eid,
                })
            yield GraphEdge("CALLS", cal.label, {"id": cal.id}, hit.label, {"id": hit.eid},
                            {"resolution": "external", **hit.extra})
            continue
        target_id, target_label, extra = hit
        if target_id == cal.id or target_id in emitted:
            continue
        emitted.add(target_id)
        yield GraphEdge("CALLS", cal.label, {"id": cal.id}, target_label, {"id": target_id},
                        {"resolution": "name", **extra})


def _depends_on(source_label: str, source_id: str, type_texts, type_index, skip_type_id: str | None,
                from_module: str | None = None) -> Iterator[GraphEdge]:
    names: set[str] = set()
    for tt in type_texts:
        names.update(_referenced_names(tt))
    emitted: set[str] = set()
    for name in names:
        target = _resolve_target(name, type_index, from_module)
        if target is None or target.type_id in emitted or target.type_id == skip_type_id:
            continue
        emitted.add(target.type_id)
        yield GraphEdge("DEPENDS_ON", source_label, {"id": source_id}, target.kind, {"id": target.type_id})


# -- imports -------------------------------------------------------------------------------

def _emit_imports(rf, file_id: str, emitted_modules: set[str]) -> Iterator[GraphNode | GraphEdge]:
    """One IMPORTS edge per `import` to an external module placeholder (ADR 0004). A Swift module
    is a whole framework/package, essentially always out-of-project, so imported modules are
    placeholder `Module` nodes keyed by their cross-repo linkage; a later ingest of that package
    could upgrade them. Deduped across the whole pass so N importers share one Module node."""
    seen_here: set[str] = set()
    for imp in rf.imports:
        if not imp.name or imp.name in seen_here:
            continue
        seen_here.add(imp.name)
        eid = ids.extern_module_key(imp.name)
        if eid not in emitted_modules:
            emitted_modules.add(eid)
            yield GraphNode("Module", {"id": eid}, {
                "name": imp.name, "resolved": False,
                "origin": "stdlib" if imp.name in _SYSTEM_MODULES else "external",
                "linkage_key": eid,
            })
        yield GraphEdge("IMPORTS", "File", {"id": file_id}, "Module", {"id": eid}, {"resolution": "external"})


# -- helpers -------------------------------------------------------------------------------

def _norm(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def _basename(path: str) -> str:
    return _norm(path).rsplit("/", 1)[-1]
