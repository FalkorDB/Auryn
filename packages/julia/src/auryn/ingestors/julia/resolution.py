"""Project-wide pass (phase 2): turn the per-file RawFile list into graph nodes/edges, applying
the cross-scope decisions a single file can't -- materializing the module tier, grouping the
concrete definitions that share a (scope, name) under one generic Function, resolving the
subtype hierarchy (EXTENDS), multiple-dispatch targets (DISPATCHES_ON), type references
(DEPENDS_ON), and best-effort calls (CALLS).

Julia's model, faithfully (there are NO classes -- it's modules + multiple dispatch):
- The tier is `Module` (`module X ... end`); items declared outside any module hang off the
  Project directly (Julia files are not modules). Nested modules chain via HAS_SUBMODULE.
- Types are `Struct` / `AbstractType` / `PrimitiveType`. `<:` becomes EXTENDS (to a project type).
- Each concrete definition is a `Method`; the Methods that share a (scope, name) are grouped
  under one `Function` (the generic function) via HAS_METHOD. This is the distinctive, correct
  Julia choice: `area(::Circle)` and `area(::Square)` are two Methods of the one Function `area`.
- `DISPATCHES_ON` (Method -> type) is the Julia-specific signal: the project types a method's
  parameter annotations dispatch on (dispatch is on argument types, not a receiver).

Name-based only -- this is the fast tree-sitter engine, so every CALLS edge is stamped
`resolution="name"` and points a calling Method at the callee's *generic Function* (which
concrete method a call actually dispatches to needs Julia's type inference -- the deferred deep
pass). Ambiguous references resolve deterministically to one target or, when nothing in the
project matches, produce no edge rather than a wrong guess -- the same trade-off as Java/C#."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass

from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.ingestors.julia import ids
from auryn.ingestors.julia.raw_model import RawFile, RawMethod, RawModule, RawType

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_!]*")
# Tokens that appear in type text but never name a project type.
_NON_TYPE_TOKENS = frozenset({"where", "Union", "Type", "Vararg"})

# ADR 0004: a best-effort small set of well-known Julia Base/stdlib names, so a call to one is
# surfaced with origin="stdlib" (a terminal external that never resolves to an ingested repo)
# rather than "external" (a candidate a sibling repo could define). Keyed by the callee's simple
# name (`println` for `Base.println(...)`).
_STDLIB = frozenset({
    "println", "print", "push!", "pop!", "length", "size", "map", "filter",
    "reduce", "sum", "collect", "zeros", "ones", "rand", "string", "parse",
    "error", "@assert",
})


def _julia_origin(name: str) -> str:
    return "stdlib" if name in _STDLIB else "external"


@dataclass
class _TypeEntry:
    type_id: str
    raw: RawType
    scope_id: str
    scope_label: str  # "Module" | "Project"
    file_id: str


@dataclass
class _MethodEntry:
    scope_id: str
    scope_label: str
    file_id: str
    raw: RawMethod


@dataclass
class _ModuleEmit:
    mid: str
    name: str
    qualified: str
    parent_id: str
    parent_label: str  # "Project" | "Module"
    rel: str  # "HAS_MODULE" | "HAS_SUBMODULE"
    raw: RawModule


def resolve(raw_files: list[RawFile], project_id: str) -> Iterator[GraphNode | GraphEdge]:
    # -- files -----------------------------------------------------------------------------
    file_ids: dict[str, str] = {}
    for rf in raw_files:
        fid = ids.file_id(project_id, rf.path)
        file_ids[rf.path] = fid
        yield GraphNode("File", {"id": fid}, {"name": _basename(rf.path), "path": _norm(rf.path)})
        yield GraphEdge("HAS_FILE", "Project", {"id": project_id}, "File", {"id": fid})

    # -- walk module tiers; collect type/method entries and module emissions ---------------
    type_entries: list[_TypeEntry] = []
    method_entries: list[_MethodEntry] = []
    module_emissions: list[_ModuleEmit] = []
    for rf in raw_files:
        _walk(rf, project_id, "Project", file_ids[rf.path], "", project_id,
              type_entries, method_entries, module_emissions)

    module_qual_by_id: dict[str, str] = {project_id: ""}
    seen_modules: set[str] = set()
    for me in module_emissions:
        module_qual_by_id.setdefault(me.mid, me.qualified)
        if me.mid in seen_modules:
            continue
        seen_modules.add(me.mid)
        yield GraphNode("Module", {"id": me.mid}, {
            "name": me.name, "qualified_name": me.qualified,
            "source": me.raw.source, "doc": me.raw.doc,
            "line_start": me.raw.line_start, "line_end": me.raw.line_end,
        })
        yield GraphEdge(me.rel, me.parent_label, {"id": me.parent_id}, "Module", {"id": me.mid})

    # -- indexes ---------------------------------------------------------------------------
    type_index: dict[str, list[_TypeEntry]] = defaultdict(list)
    for te in type_entries:
        type_index[te.raw.name].append(te)

    functions: dict[tuple[str, str], tuple[str, str, str]] = {}  # (scope_id, name) -> (func_id, scope_id, scope_label)
    function_index: dict[str, list[tuple[str, str]]] = defaultdict(list)  # name -> [(scope_id, func_id)]
    for me in method_entries:
        key = (me.scope_id, me.raw.name)
        if key not in functions:
            fid = ids.function_id(me.scope_id, me.raw.name)
            functions[key] = (fid, me.scope_id, me.scope_label)
            function_index[me.raw.name].append((me.scope_id, fid))

    # -- pass 1: nodes + structural edges --------------------------------------------------
    for te in type_entries:
        yield from _emit_type(te)

    for (scope_id, name), (fid, _sid, scope_label) in functions.items():
        qual = module_qual_by_id.get(scope_id, "")
        qualified = f"{qual}.{name}" if qual else name
        yield GraphNode("Function", {"id": fid}, {
            "name": name, "qualified_name": qualified,
            # ADR 0004: cross-repo key on every real generic function, so a placeholder in a sibling
            # repo that calls it (by the same qualified name) heals onto this node when they share a graph.
            "linkage_key": ids.extern_function_key(qualified),
        })
        yield GraphEdge("HAS_FUNCTION", scope_label, {"id": scope_id}, "Function", {"id": fid})

    for me in method_entries:
        fid = functions[(me.scope_id, me.raw.name)][0]
        yield from _emit_method(fid, me)

    # -- pass 2: cross references ----------------------------------------------------------
    # Simple names of the project's modules -- used to keep an intra-project dangling call
    # (`ProjMod.gone(...)` into a module that exists but lacks the function) from becoming a
    # cross-repo placeholder (ADR 0004 guard).
    project_modules = {me.name for me in module_emissions}
    emitted_externs: set[str] = set()  # placeholder ids already emitted this pass (dedup)
    for te in type_entries:
        yield from _emit_extends(te, type_index)
        yield from _emit_field_depends(te, type_index)
    for me in method_entries:
        fid = functions[(me.scope_id, me.raw.name)][0]
        mid = ids.method_id(fid, [p.type for p in me.raw.parameters])
        yield from _emit_dispatch_and_depends(mid, me, type_index)
        yield from _emit_calls(mid, me, function_index, project_modules, emitted_externs)


# -- walk ----------------------------------------------------------------------------------

def _submodules(container) -> list[RawModule]:
    return container.submodules if isinstance(container, RawModule) else container.modules


def _walk(container, scope_id: str, scope_label: str, file_id: str, qual_prefix: str, project_id: str,
          type_entries: list[_TypeEntry], method_entries: list[_MethodEntry],
          module_emissions: list[_ModuleEmit]) -> None:
    for rt in container.types:
        tid = ids.type_id(scope_id, rt.kind, rt.name, rt.type_parameters)
        type_entries.append(_TypeEntry(tid, rt, scope_id, scope_label, file_id))
    for rm in container.functions:
        method_entries.append(_MethodEntry(scope_id, scope_label, file_id, rm))
    for sub in _submodules(container):
        qual = f"{qual_prefix}.{sub.name}" if qual_prefix else sub.name
        mid = ids.module_id(project_id, qual)
        if scope_label == "Project":
            module_emissions.append(_ModuleEmit(mid, sub.name, qual, project_id, "Project", "HAS_MODULE", sub))
        else:
            module_emissions.append(_ModuleEmit(mid, sub.name, qual, scope_id, "Module", "HAS_SUBMODULE", sub))
        _walk(sub, mid, "Module", file_id, qual, project_id, type_entries, method_entries, module_emissions)


# -- pass 1 emitters -----------------------------------------------------------------------

def _emit_type(te: _TypeEntry) -> Iterator[GraphNode | GraphEdge]:
    rt = te.raw
    props: dict = {
        "name": rt.name,
        "type_parameters": list(rt.type_parameters),
        "source": rt.source,
        "doc": rt.doc,
        "line_start": rt.line_start,
        "line_end": rt.line_end,
    }
    if rt.kind == "Struct":
        props["is_mutable"] = rt.is_mutable
    elif rt.kind == "PrimitiveType":
        props["bits"] = rt.bits
    yield GraphNode(rt.kind, {"id": te.type_id}, props)
    yield GraphEdge("HAS_TYPE", te.scope_label, {"id": te.scope_id}, rt.kind, {"id": te.type_id})
    yield GraphEdge("DEFINED_IN", rt.kind, {"id": te.type_id}, "File", {"id": te.file_id})
    for f in rt.fields:
        fid = ids.field_id(te.type_id, f.name)
        yield GraphNode("Field", {"id": fid}, {
            "name": f.name, "type": f.type, "line_start": f.line_start, "line_end": f.line_end,
        })
        yield GraphEdge("HAS_FIELD", rt.kind, {"id": te.type_id}, "Field", {"id": fid})


def _emit_method(func_id: str, me: _MethodEntry) -> Iterator[GraphNode | GraphEdge]:
    m = me.raw
    mid = ids.method_id(func_id, [p.type for p in m.parameters])
    yield GraphNode("Method", {"id": mid}, {
        "name": m.name,
        "is_short": m.is_short,
        "return_type": m.return_type,
        "type_parameters": list(m.type_parameters),
        "parameter_types": [p.type for p in m.parameters],
        "source": m.source,
        "doc": m.doc,
        "line_start": m.line_start,
        "line_end": m.line_end,
    })
    yield GraphEdge("HAS_METHOD", "Function", {"id": func_id}, "Method", {"id": mid})
    yield GraphEdge("DEFINED_IN", "Method", {"id": mid}, "File", {"id": me.file_id})
    for i, p in enumerate(m.parameters):
        pid = ids.parameter_id(mid, i, p.name)
        yield GraphNode("Parameter", {"id": pid}, {
            "name": p.name, "type": p.type, "position": p.position,
            "is_vararg": p.is_vararg, "is_keyword": p.is_keyword,
        })
        yield GraphEdge("HAS_PARAMETER", "Method", {"id": mid}, "Parameter", {"id": pid})


# -- pass 2 emitters -----------------------------------------------------------------------

def _resolve_type(name: str, type_index: dict[str, list[_TypeEntry]], scope_id: str) -> _TypeEntry | None:
    matches = type_index.get(name)
    if not matches:
        return None
    same = [m for m in matches if m.scope_id == scope_id]
    pool = same or matches
    return sorted(pool, key=lambda m: m.type_id)[0]  # deterministic on ambiguity


def _type_names(type_text: str | None) -> list[str]:
    if not type_text:
        return []
    return [n for n in _IDENTIFIER_RE.findall(type_text) if n not in _NON_TYPE_TOKENS]


def _emit_extends(te: _TypeEntry, type_index) -> Iterator[GraphEdge]:
    if not te.raw.supertype:
        return
    target = _resolve_type(te.raw.supertype, type_index, te.scope_id)
    if target is not None and target.type_id != te.type_id:
        yield GraphEdge("EXTENDS", te.raw.kind, {"id": te.type_id}, target.raw.kind, {"id": target.type_id})


def _emit_field_depends(te: _TypeEntry, type_index) -> Iterator[GraphEdge]:
    for f in te.raw.fields:
        fid = ids.field_id(te.type_id, f.name)
        emitted: set[str] = set()
        for nm in _type_names(f.type):
            target = _resolve_type(nm, type_index, te.scope_id)
            if target is None or target.type_id == te.type_id or target.type_id in emitted:
                continue
            emitted.add(target.type_id)
            yield GraphEdge("DEPENDS_ON", "Field", {"id": fid}, target.raw.kind, {"id": target.type_id})


def _emit_dispatch_and_depends(mid: str, me: _MethodEntry, type_index) -> Iterator[GraphEdge]:
    dispatched: set[str] = set()
    for p in me.raw.parameters:
        for nm in _type_names(p.type):
            target = _resolve_type(nm, type_index, me.scope_id)
            if target is None or target.type_id in dispatched:
                continue
            dispatched.add(target.type_id)
            yield GraphEdge("DISPATCHES_ON", "Method", {"id": mid}, target.raw.kind, {"id": target.type_id})
    # DEPENDS_ON: every project type referenced by a parameter OR the return annotation.
    depended: set[str] = set()
    ref_names: list[str] = []
    for p in me.raw.parameters:
        ref_names.extend(_type_names(p.type))
    ref_names.extend(_type_names(me.raw.return_type))
    for nm in ref_names:
        target = _resolve_type(nm, type_index, me.scope_id)
        if target is None or target.type_id in depended:
            continue
        depended.add(target.type_id)
        yield GraphEdge("DEPENDS_ON", "Method", {"id": mid}, target.raw.kind, {"id": target.type_id})


def _resolve_call(name: str, function_index: dict[str, list[tuple[str, str]]], scope_id: str) -> str | None:
    cands = function_index.get(name)
    if not cands:
        return None
    same = [c for c in cands if c[0] == scope_id]
    pool = same or cands
    return sorted(pool, key=lambda c: c[1])[0][1]  # deterministic func_id


def _qualified_call_name(call) -> str:
    """What the consumer wrote (the linkage-key basis, ADR 0004): `Base.println` for
    `Base.println(...)`, `MyMod.compute` for `MyMod.compute(...)`, bare `push!` for `push!(...)`."""
    return f"{call.receiver}.{call.name}" if call.receiver else call.name


def _external_function_node(call, qualified: str, eid: str) -> GraphNode:
    """A placeholder for an unresolved (nameable) call, keyed by its cross-repo linkage so a later
    ingest of the defining repo takes it over (ADR 0004). Shallow: shape mirrors a real generic
    Function so the same CALLS queries reach it; `resolved:false` + `origin` mark it."""
    return GraphNode("Function", {"id": eid}, {
        "name": call.name, "qualified_name": qualified, "resolved": False,
        "origin": _julia_origin(call.name), "linkage_key": eid,
    })


def _emit_calls(mid: str, me: _MethodEntry, function_index, project_modules: set[str],
                emitted_externs: set[str]) -> Iterator[GraphNode | GraphEdge]:
    emitted: set[str] = set()
    for call in me.raw.calls:
        target = _resolve_call(call.name, function_index, me.scope_id)
        if target is not None:
            if target in emitted:
                continue
            emitted.add(target)
            yield GraphEdge("CALLS", "Method", {"id": mid}, "Function", {"id": target}, {"resolution": "name"})
            continue
        # Unresolved: the callee names no project generic function. A module-qualified callee whose
        # receiver IS a project module is an intra-project dangling ref (the module lacks the
        # function) -> leave it dropped, not a cross-repo placeholder. Everything else nameable --
        # Base/stdlib (`println`, `push!`) and out-of-project calls -- becomes a first-class
        # placeholder Function keyed by its cross-repo linkage, reached by an ordinary CALLS edge.
        if call.receiver and call.receiver in project_modules:
            continue
        qualified = _qualified_call_name(call)
        eid = ids.extern_function_key(qualified)
        if eid in emitted:
            continue
        emitted.add(eid)
        if eid not in emitted_externs:
            emitted_externs.add(eid)
            yield _external_function_node(call, qualified, eid)
        yield GraphEdge("CALLS", "Method", {"id": mid}, "Function", {"id": eid}, {"resolution": "external"})


# -- helpers -------------------------------------------------------------------------------

def _norm(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def _basename(path: str) -> str:
    return _norm(path).rsplit("/", 1)[-1]
