"""Project-wide pass (phase 2): turn the per-file RawFile list into graph nodes/edges, applying
the cross-file decisions a single file can't -- materializing the namespace tier, resolving
EXTENDS/IMPLEMENTS/USES_TRAIT and parameter/return/property type references to project types, and
best-effort call resolution.

Adapted from the Java ingestor's resolution (PHP is its structural sibling: explicit classes/
interfaces with extends/implements), extended for PHP: the tier is `Namespace` (backslash-
separated); there is a `Trait` type kind and a `USES_TRAIT` edge; class `Property`/`Constant`
members and enum `EnumCase`s; and top-level free `Function`s (indexed so a bare `foo()` resolves,
Python/C-style) alongside methods.

Name-based only -- this is the fast tree-sitter engine, so every CALLS edge is stamped
`resolution="name"`. Full PHP name-resolution (use-statement aliasing, autoloading/PSR-4,
trait-flattened and inherited-member call targets) is the deferred deep pass; a future deep engine
would re-emit the same node ids with a stronger provenance (the two-phase model). An ambiguous
call still produces no edge rather than a wrong guess, but a *nameable* unresolved call (a
builtin, a vendor/other-repo callable, or a not-yet-ingested sibling repo's symbol) becomes a
first-class placeholder node reached by an ordinary CALLS edge, keyed by its cross-repo
`linkage_key` so it heals to the real definition when that repo joins the graph (ADR 0004)."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass

from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.ingestors.php import ids
from auryn.ingestors.php.raw_model import RawFile, RawFunction, RawMethod, RawType

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Type tokens that never name a project type -- PHP built-in types/keywords in type text.
_NON_TYPE_TOKENS = frozenset(
    {"int", "integer", "float", "double", "string", "bool", "boolean", "void", "array",
     "object", "mixed", "callable", "iterable", "null", "false", "true", "never", "self",
     "static", "parent", "this"}
)

# ADR 0004: a best-effort set of common PHP built-in functions, so a bare call to one is surfaced
# with origin="builtin" (a terminal external that never resolves to an ingested repo) rather than
# "external" (a candidate a sibling repo could define). Not exhaustive -- the fast engine can't
# know the full internal-function list; a future deep pass would refine it.
_PHP_BUILTINS = frozenset(
    {"strlen", "str_replace", "count", "array_map", "array_filter", "implode", "explode",
     "json_encode", "json_decode", "isset", "printf", "sprintf", "preg_match", "in_array",
     "array_keys", "array_values"}
)


@dataclass
class _Entry:
    type_id: str
    kind: str  # "Class" | "Interface" | "Trait" | "Enum"
    name: str
    namespace: str
    raw_type: RawType
    file_id: str
    scope_id: str
    scope_label: str  # "Namespace" | "Project"


@dataclass
class _FuncEntry:
    func_id: str
    name: str
    namespace: str
    raw_func: RawFunction
    file_id: str
    scope_id: str
    scope_label: str  # "Namespace" | "Project"


def resolve(raw_files: list[RawFile], project_id: str) -> Iterator[GraphNode | GraphEdge]:
    file_ids: dict[str, str] = {}
    namespaces_seen: set[str] = set()
    for rf in raw_files:
        fid = ids.file_id(project_id, rf.path)
        file_ids[rf.path] = fid
        yield GraphNode("File", {"id": fid}, {"name": _basename(rf.path), "path": _norm(rf.path)})
        yield GraphEdge("HAS_FILE", "Project", {"id": project_id}, "File", {"id": fid})
        for rt in rf.types:
            if rt.namespace:
                namespaces_seen.add(rt.namespace)
        for fn in rf.functions:
            if rf.namespace:
                namespaces_seen.add(rf.namespace)
    yield from _emit_namespaces(namespaces_seen, project_id)

    entries: list[_Entry] = []
    func_entries: list[_FuncEntry] = []
    for rf in raw_files:
        for rt in rf.types:
            scope_id, scope_label = _scope(project_id, rt.namespace)
            entries.append(_Entry(
                ids.type_id(scope_id, rt.kind, rt.name), rt.kind, rt.name, rt.namespace, rt,
                file_ids[rf.path], scope_id, scope_label,
            ))
        for fn in rf.functions:
            scope_id, scope_label = _scope(project_id, rf.namespace)
            func_entries.append(_FuncEntry(
                ids.function_id(scope_id, fn.name, [p.type for p in fn.parameters]),
                fn.name, rf.namespace, fn, file_ids[rf.path], scope_id, scope_label,
            ))

    type_index: dict[str, list[_Entry]] = defaultdict(list)
    for e in entries:
        type_index[e.name].append(e)

    method_index: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for e in entries:
        for m in e.raw_type.methods:
            method_index[m.name].append((e.type_id, _method_id(e.type_id, m), e.namespace))

    function_index: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for fe in func_entries:
        function_index[fe.name].append((fe.func_id, fe.namespace))

    # Pass 1: nodes + structural edges (so pass 2's cross-refs always have a landing node).
    for e in entries:
        yield from _emit_type_and_members(e)
    for fe in func_entries:
        yield from _emit_function(fe)
    # Pass 2: cross-references. `emitted_externs` dedups placeholder nodes across the whole pass
    # (ADR 0004) so every caller of the same missing symbol MERGEs onto one node.
    emitted_externs: set[str] = set()
    for e in entries:
        yield from _emit_hierarchy(e, type_index)
        yield from _emit_type_references(e, type_index)
        yield from _emit_calls(e, type_index, method_index, function_index, emitted_externs)
    for fe in func_entries:
        yield from _emit_function_references(fe, type_index)
        yield from _emit_function_calls(fe, type_index, method_index, function_index, emitted_externs)


# -- scope / namespaces --------------------------------------------------------------------

def _scope(project_id: str, namespace: str) -> tuple[str, str]:
    if namespace:
        return ids.namespace_id(project_id, namespace), "Namespace"
    return project_id, "Project"


def _emit_namespaces(namespaces: set[str], project_id: str) -> Iterator[GraphNode | GraphEdge]:
    """Materialize every prefix of every declared namespace as a Namespace node (as C#/Java do):
    the first segment hangs off the Project via HAS_NAMESPACE, deeper segments chain via
    HAS_SUBNAMESPACE. `App\\Models` -> Namespace(App) -> Namespace(App\\Models)."""
    emitted: set[str] = set()
    for namespace in namespaces:
        prefix = ""
        parent_id, parent_label, parent_rel = project_id, "Project", "HAS_NAMESPACE"
        for segment in namespace.split("\\"):
            prefix = f"{prefix}\\{segment}" if prefix else segment
            nid = ids.namespace_id(project_id, prefix)
            if nid not in emitted:
                emitted.add(nid)
                yield GraphNode("Namespace", {"id": nid}, {"name": segment, "qualified_name": prefix})
                yield GraphEdge(parent_rel, parent_label, {"id": parent_id}, "Namespace", {"id": nid})
            parent_id, parent_label, parent_rel = nid, "Namespace", "HAS_SUBNAMESPACE"


# -- pass 1: nodes + structural edges ------------------------------------------------------

def _type_properties(rt: RawType) -> dict:
    props: dict = {
        "name": rt.name,
        "qualified_name": f"{rt.namespace}\\{rt.name}" if rt.namespace else rt.name,
        "namespace": rt.namespace,
        "source": rt.source,
        "doc": rt.doc,
        "line_start": rt.line_start,
        "line_end": rt.line_end,
    }
    if rt.kind == "Class":
        props.update(is_abstract=rt.is_abstract, is_final=rt.is_final)
    elif rt.kind == "Enum":
        props["backing_type"] = rt.backing_type
    return props


def _emit_type_and_members(e: _Entry) -> Iterator[GraphNode | GraphEdge]:
    yield GraphNode(e.kind, {"id": e.type_id}, _type_properties(e.raw_type))
    yield GraphEdge("HAS_TYPE", e.scope_label, {"id": e.scope_id}, e.kind, {"id": e.type_id})
    yield GraphEdge("DEFINED_IN", e.kind, {"id": e.type_id}, "File", {"id": e.file_id})

    type_qname = f"{e.namespace}\\{e.raw_type.name}" if e.namespace else e.raw_type.name
    for m in e.raw_type.methods:
        yield from _emit_method(e.type_id, e.kind, type_qname, m)
    for p in e.raw_type.properties:
        yield from _emit_property(e.type_id, e.kind, p)
    for k in e.raw_type.constants:
        cid = ids.constant_id(e.type_id, k.name)
        yield GraphNode("Constant", {"id": cid}, {"name": k.name, "value": k.value, "visibility": k.visibility})
        yield GraphEdge("HAS_CONSTANT", e.kind, {"id": e.type_id}, "Constant", {"id": cid})
    for case in e.raw_type.enum_cases:
        caseid = ids.enum_case_id(e.type_id, case.name)
        yield GraphNode("EnumCase", {"id": caseid}, {"name": case.name, "value": case.value})
        yield GraphEdge("HAS_ENUM_CASE", "Enum", {"id": e.type_id}, "EnumCase", {"id": caseid})


def _method_id(type_id: str, m: RawMethod) -> str:
    return ids.method_id(type_id, m.name, [p.type for p in m.parameters])


def _emit_method(type_id: str, type_label: str, type_qname: str, m: RawMethod) -> Iterator[GraphNode | GraphEdge]:
    mid = _method_id(type_id, m)
    yield GraphNode("Method", {"id": mid}, {
        "name": m.name,
        "kind": m.kind,
        "visibility": m.visibility,
        "is_static": m.is_static,
        "is_abstract": m.is_abstract,
        "is_final": m.is_final,
        "return_type": m.return_type,
        "source": m.source,
        "doc": m.doc,
        "line_start": m.line_start,
        "line_end": m.line_end,
        # Cross-repo key (ADR 0004): the name a consumer writes -- `App\\Billing::charge`.
        "linkage_key": ids.extern_function_key(f"{type_qname}::{m.name}"),
    })
    yield GraphEdge("HAS_METHOD", type_label, {"id": type_id}, "Method", {"id": mid})
    for i, p in enumerate(m.parameters):
        yield from _emit_parameter(mid, "Method", p, i)


def _emit_function(fe: _FuncEntry) -> Iterator[GraphNode | GraphEdge]:
    fn = fe.raw_func
    qualified = f"{fe.namespace}\\{fn.name}" if fe.namespace else fn.name
    yield GraphNode("Function", {"id": fe.func_id}, {
        "name": fn.name,
        "qualified_name": qualified,
        "return_type": fn.return_type,
        "source": fn.source,
        "doc": fn.doc,
        "line_start": fn.line_start,
        "line_end": fn.line_end,
        # Cross-repo key (ADR 0004): the name a consumer writes -- `App\\helper` / `helper`.
        "linkage_key": ids.extern_function_key(qualified),
    })
    yield GraphEdge("HAS_FUNCTION", fe.scope_label, {"id": fe.scope_id}, "Function", {"id": fe.func_id})
    yield GraphEdge("DEFINED_IN", "Function", {"id": fe.func_id}, "File", {"id": fe.file_id})
    for i, p in enumerate(fn.parameters):
        yield from _emit_parameter(fe.func_id, "Function", p, i)


def _emit_parameter(parent_id: str, parent_label: str, param, position: int) -> Iterator[GraphNode | GraphEdge]:
    pid = ids.parameter_id(parent_id, position, param.name)
    yield GraphNode("Parameter", {"id": pid}, {
        "name": param.name, "type": param.type, "position": param.position,
        "is_variadic": param.is_variadic, "is_promoted": param.is_promoted,
    })
    yield GraphEdge("HAS_PARAMETER", parent_label, {"id": parent_id}, "Parameter", {"id": pid})


def _emit_property(type_id: str, type_label: str, p) -> Iterator[GraphNode | GraphEdge]:
    pid = ids.property_id(type_id, p.name)
    yield GraphNode("Property", {"id": pid}, {
        "name": p.name, "type": p.type, "visibility": p.visibility,
        "is_static": p.is_static, "is_readonly": p.is_readonly, "is_promoted": p.is_promoted,
    })
    yield GraphEdge("HAS_PROPERTY", type_label, {"id": type_id}, "Property", {"id": pid})


# -- pass 2: hierarchy, type references, calls ---------------------------------------------

def _resolve_target(name: str, type_index: dict[str, list[_Entry]], from_namespace: str) -> _Entry | None:
    matches = type_index.get(name)
    if not matches:
        return None
    same_ns = [m for m in matches if m.namespace == from_namespace]
    pool = same_ns or matches
    return sorted(pool, key=lambda m: m.type_id)[0]  # deterministic on ambiguity


def _emit_hierarchy(e: _Entry, type_index: dict[str, list[_Entry]]) -> Iterator[GraphEdge]:
    rt = e.raw_type
    if rt.superclass:  # class -> class
        target = _resolve_target(rt.superclass, type_index, e.namespace)
        if target is not None and target.type_id != e.type_id:
            yield GraphEdge("EXTENDS", e.kind, {"id": e.type_id}, target.kind, {"id": target.type_id})
    for iface in rt.interfaces:
        target = _resolve_target(iface, type_index, e.namespace)
        if target is None or target.type_id == e.type_id:
            continue
        # an interface's `interfaces` list is really its super-interfaces -> EXTENDS.
        rel = "EXTENDS" if e.kind == "Interface" else "IMPLEMENTS"
        yield GraphEdge(rel, e.kind, {"id": e.type_id}, target.kind, {"id": target.type_id})
    for trait in rt.traits:
        target = _resolve_target(trait, type_index, e.namespace)
        if target is not None and target.type_id != e.type_id:
            yield GraphEdge("USES_TRAIT", e.kind, {"id": e.type_id}, target.kind, {"id": target.type_id})


def _referenced_names(type_text: str | None) -> list[str]:
    if not type_text:
        return []
    return [n for n in _IDENTIFIER_RE.findall(type_text) if n not in _NON_TYPE_TOKENS]


def _emit_type_references(e: _Entry, type_index: dict[str, list[_Entry]]) -> Iterator[GraphEdge]:
    for m in e.raw_type.methods:
        mid = _method_id(e.type_id, m)
        names = set(_referenced_names(m.return_type))
        for p in m.parameters:
            names.update(_referenced_names(p.type))
        yield from _depends_on("Method", mid, names, e.namespace, e.type_id, type_index)
    for p in e.raw_type.properties:
        pid = ids.property_id(e.type_id, p.name)
        yield from _depends_on("Property", pid, set(_referenced_names(p.type)), e.namespace, e.type_id, type_index)


def _emit_function_references(fe: _FuncEntry, type_index: dict[str, list[_Entry]]) -> Iterator[GraphEdge]:
    fn = fe.raw_func
    names = set(_referenced_names(fn.return_type))
    for p in fn.parameters:
        names.update(_referenced_names(p.type))
    yield from _depends_on("Function", fe.func_id, names, fe.namespace, None, type_index)


def _depends_on(from_label: str, from_id: str, names: set[str], from_namespace: str,
                self_type_id: str | None, type_index) -> Iterator[GraphEdge]:
    emitted: set[str] = set()
    for name in names:
        target = _resolve_target(name, type_index, from_namespace)
        if target is None or target.type_id == self_type_id or target.type_id in emitted:
            continue
        emitted.add(target.type_id)
        yield GraphEdge("DEPENDS_ON", from_label, {"id": from_id}, target.kind, {"id": target.type_id})


def _pick(candidates: list[tuple[str, str, str]]) -> str:
    return sorted(candidates, key=lambda c: c[1])[0][1]


def _same_type_then_ns(cands, from_type_id: str | None, from_namespace: str):
    if from_type_id is not None:
        same_type = [c for c in cands if c[0] == from_type_id]
        if same_type:
            return "Method", _pick(same_type), {}
    same_ns = [c for c in cands if c[2] == from_namespace]
    return "Method", _pick(same_ns or cands), {}


def _resolve_call(call, from_type_id: str | None, from_namespace: str, receiver_types: dict[str, str],
                  type_index, method_index, function_index):
    """(target_label, target_id, extra_props) or None.

    `new X()` -> X's `__construct`; a bare `foo()` -> a project Function (same-namespace first);
    `$this->m()`/`self::m()` -> the same type, then same namespace; a receiver naming a known
    property/param prefers that declared type's method; `Foo::m()` -> Foo's method directly."""
    if call.kind == "new":
        target = _resolve_target(call.name, type_index, from_namespace)
        if target is not None:
            ctor = next((m for m in target.raw_type.methods if m.kind == "CONSTRUCTOR"), None)
            if ctor is not None:
                return "Method", _method_id(target.type_id, ctor), {}
        return None

    if call.kind == "function":
        cands = function_index.get(call.name)
        if not cands:
            return None
        same_ns = [c for c in cands if c[1] == from_namespace]
        return "Function", sorted(same_ns or cands, key=lambda c: c[0])[0][0], {}

    cands = method_index.get(call.name)
    if not cands:
        return None

    if call.kind == "static":
        recv = call.receiver
        if recv in (None, "self", "static", "parent"):
            return _same_type_then_ns(cands, from_type_id, from_namespace)
        target_type = _resolve_target(recv, type_index, from_namespace)
        if target_type is not None:
            matches = [c for c in cands if c[0] == target_type.type_id]
            if matches:
                return "Method", _pick(matches), {"receiver_type": target_type.name}
        return None

    # instance (member) call
    recv = call.receiver
    if recv in (None, "this"):
        return _same_type_then_ns(cands, from_type_id, from_namespace)
    rtype = receiver_types.get(recv)
    if rtype is not None:
        for type_name in _referenced_names(rtype):
            target_type = _resolve_target(type_name, type_index, from_namespace)
            if target_type is None:
                continue
            matches = [c for c in cands if c[0] == target_type.type_id]
            if matches:
                return "Method", _pick(matches), {"receiver_type": target_type.name}
        return None  # resolved the receiver's type, but it has no such method
    same_ns = [c for c in cands if c[2] == from_namespace]  # unknown local var -> name-based guess
    return "Method", _pick(same_ns or cands), {}


def _emit_callable_calls(from_label, from_id, from_type_id, from_namespace, calls, receiver_types,
                         type_index, method_index, function_index, emitted_externs) -> Iterator[GraphNode | GraphEdge]:
    emitted: set[str] = set()
    for call in calls:
        hit = _resolve_call(call, from_type_id, from_namespace, receiver_types, type_index, method_index, function_index)
        if hit is not None:
            target_label, target_id, extra = hit
            if target_id in emitted:
                continue
            emitted.add(target_id)
            yield GraphEdge("CALLS", from_label, {"id": from_id}, target_label, {"id": target_id}, {"resolution": "name", **extra})
            continue
        # Unresolved (ADR 0004): a PHP built-in, a vendor/other-repo callable, or a sibling repo's
        # symbol not yet ingested. When it's nameable, surface a placeholder reached by an ordinary
        # CALLS edge so the reference exists in the graph and heals when the definer is ingested;
        # dynamic/unknown-receiver and intra-project-dangling calls stay dropped.
        ext = _external_placeholder(call, from_namespace, receiver_types, type_index)
        if ext is None:
            continue
        label, qualified, short, origin = ext
        eid = ids.extern_function_key(qualified)
        if eid in emitted:
            continue
        emitted.add(eid)
        if eid not in emitted_externs:  # one placeholder node per missing symbol; edges per caller
            emitted_externs.add(eid)
            yield GraphNode(label, {"id": eid}, {
                "name": short, "qualified_name": qualified,
                "resolved": False, "origin": origin, "linkage_key": eid,
            })
        yield GraphEdge("CALLS", from_label, {"id": from_id}, label, {"id": eid}, {"resolution": "external"})


def _qualify_class(name_or_type: str, from_namespace: str) -> str | None:
    """The cross-repo class name a consumer's receiver denotes -- so a placeholder's key lines up
    with the real class's `linkage_key` (`App\\Billing`). An already-qualified reference
    (`\\Vendor\\Client`, `App\\Models\\Store`) keeps its path (leading `\\` dropped); a bare name
    resolves against the caller's namespace (PHP name resolution: `Billing` in `App` -> `App\\Billing`).
    Union/nullable text is reduced to its first alternative. None if nothing nameable remains."""
    first = name_or_type.split("|")[0].strip().lstrip("?").strip().lstrip("\\")
    if not first or first in _NON_TYPE_TOKENS:
        return None
    if "\\" in first:  # already namespace-qualified -> the path IS the FQN
        return first
    return f"{from_namespace}\\{first}" if from_namespace else first


def _external_placeholder(call, from_namespace: str, receiver_types: dict[str, str], type_index):
    """A call that did NOT resolve to a project node -> (label, qualified_name, short_name, origin)
    for a placeholder (ADR 0004), or None to leave it dropped.

    Nameable -> placeholder: a free/namespaced function call (`foo()`); a `Class::method` or
    `$typedvar->method()` whose receiver names a class that is NOT a project type; a
    `new ExternalClass()` constructor. Dropped: self/relative/`$this` and untyped/unknown local
    receivers (dynamic), and a missing member of a *project* class (intra-project dangling -- so
    nothing stale survives an incremental resync). PHP namespaces are open across files/repos, so a
    free function not defined here is external, never intra-project dangling. Keys are
    namespace-qualified (matching the real callables' `linkage_key`) so a cross-repo call heals."""
    if call.kind == "function":
        if call.name in _PHP_BUILTINS:  # global builtin -> terminal external, keyed by bare name
            return "Function", call.name, call.name, "builtin"
        qualified = f"{from_namespace}\\{call.name}" if from_namespace else call.name
        return "Function", qualified, call.name, "external"

    if call.kind == "new":
        if _resolve_target(call.name, type_index, from_namespace) is not None:
            return None  # project type (ctor not modelled as an edge) -> not external
        cls = _qualify_class(call.name, from_namespace)
        return ("Method", f"{cls}::__construct", "__construct", "external") if cls else None

    if call.kind == "static":
        recv = call.receiver
        if recv in (None, "self", "static", "parent"):
            return None  # relative/self scope -> intra-project (or dynamic)
        if _resolve_target(recv, type_index, from_namespace) is not None:
            return None  # project class, missing method -> intra-project dangling
        cls = _qualify_class(recv, from_namespace)
        return ("Method", f"{cls}::{call.name}", call.name, "external") if cls else None

    # instance (member) call
    recv = call.receiver
    if recv in (None, "this"):
        return None  # $this->m() unknown -> intra-project dangling
    rtype = receiver_types.get(recv)
    if not rtype:
        return None  # untyped/unknown local variable -> dynamic dispatch, unnameable
    if any(_resolve_target(n, type_index, from_namespace) is not None for n in _referenced_names(rtype)):
        return None  # receiver typed to a PROJECT class, missing method -> intra-project dangling
    cls = _qualify_class(rtype, from_namespace)
    return ("Method", f"{cls}::{call.name}", call.name, "external") if cls else None


def _emit_calls(e: _Entry, type_index, method_index, function_index, emitted_externs) -> Iterator[GraphNode | GraphEdge]:
    property_types = {p.name: p.type for p in e.raw_type.properties if p.type}
    for m in e.raw_type.methods:
        mid = _method_id(e.type_id, m)
        receiver_types = {**property_types, **{p.name: p.type for p in m.parameters if p.type}}
        yield from _emit_callable_calls(
            "Method", mid, e.type_id, e.namespace, m.calls, receiver_types,
            type_index, method_index, function_index, emitted_externs,
        )


def _emit_function_calls(fe: _FuncEntry, type_index, method_index, function_index, emitted_externs) -> Iterator[GraphNode | GraphEdge]:
    fn = fe.raw_func
    receiver_types = {p.name: p.type for p in fn.parameters if p.type}
    yield from _emit_callable_calls(
        "Function", fe.func_id, None, fe.namespace, fn.calls, receiver_types,
        type_index, method_index, function_index, emitted_externs,
    )


# -- helpers -------------------------------------------------------------------------------

def _norm(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def _basename(path: str) -> str:
    return _norm(path).rsplit("/", 1)[-1]
