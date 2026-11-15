"""Project-wide pass (phase 2): turn the per-file RawFile list into graph nodes/edges, applying
the cross-file decisions a single file can't -- materializing the package tier, emitting top-level
`Function`s/property `Field`s, resolving EXTENDS/IMPLEMENTS and parameter/return/field type
references to project types, and best-effort call resolution.

Adapted from the Java ingestor's resolution (Kotlin is its JVM sibling), with Kotlin's additions:
top-level `fun`s become `Function`s owned by the Package/Project (HAS_FUNCTION, as in the Rust
ingestor) and are candidates for a bare call; top-level `val`/`var`s become package/project-scoped
`Field`s; and, since Kotlin has no `new`, a receiver-less call whose name is a project type is
resolved as that type's constructor. EXTENDS vs IMPLEMENTS needs no naming heuristic because the
parser already split the single `superclass` (the delegation specifier written `Base()`) from the
`interfaces` list (bare specifiers).

Name-based only -- this is the fast tree-sitter engine, so every CALLS edge is stamped
`resolution="name"` (external placeholders get `resolution="external"`). A future deep Kotlin
compiler / kotlin-language-server pass would re-emit the same node ids with a stronger provenance
(the two-phase model). Ambiguous or unresolved references produce no edge rather than a wrong
guess -- the same trade-off as the Java/Rust/C# ingestors."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field

from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.ingestors.kotlin import ids, kotlinmod
from auryn.ingestors.kotlin.raw_model import RawFile, RawFunction, RawType

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Tokens that appear in type text but never name a project type (Kotlin variance/keywords). A name
# that isn't a project type simply won't be in the type_index, so this is only a fast skip.
_NON_TYPE_TOKENS = frozenset({"out", "in", "reified", "vararg", "dynamic"})

# Kotlin auto-imports these (never written as an `import`), so a call on one can still be classified
# and keyed by its real fully-qualified name even without an import. Not exhaustive -- the surface
# that shows up as a call receiver / constructor target in practice.
_KOTLIN_BUILTINS = {
    "String": "kotlin.String", "Int": "kotlin.Int", "Long": "kotlin.Long", "Short": "kotlin.Short",
    "Byte": "kotlin.Byte", "Char": "kotlin.Char", "Boolean": "kotlin.Boolean", "Float": "kotlin.Float",
    "Double": "kotlin.Double", "Any": "kotlin.Any", "Unit": "kotlin.Unit", "Nothing": "kotlin.Nothing",
    "Number": "kotlin.Number", "Array": "kotlin.Array", "Pair": "kotlin.Pair", "Triple": "kotlin.Triple",
    "List": "kotlin.collections.List", "MutableList": "kotlin.collections.MutableList",
    "Map": "kotlin.collections.Map", "MutableMap": "kotlin.collections.MutableMap",
    "Set": "kotlin.collections.Set", "MutableSet": "kotlin.collections.MutableSet",
    "Collection": "kotlin.collections.Collection", "Iterable": "kotlin.collections.Iterable",
    "Sequence": "kotlin.sequences.Sequence", "Throwable": "kotlin.Throwable",
    "Exception": "kotlin.Exception", "RuntimeException": "kotlin.RuntimeException",
}


def _origin(fqcn: str) -> str:
    """Coarse provenance for a placeholder's callee (ADR 0004 §4): a Kotlin/JDK stdlib symbol
    (never upgraded by a future ingest) vs an `external` one (a candidate a sibling repo defines)."""
    return "stdlib" if fqcn.startswith(("kotlin.", "kotlinx.", "java.", "javax.", "jdk.")) else "external"


@dataclass
class _Extern:
    """A callee the resolver could NAME but that is not defined in this ingest -- a Kotlin/JDK
    stdlib or other-repo callable. Becomes a placeholder Method keyed by its cross-repo linkage."""
    linkage: str          # extern://kotlin/<qualified> -- the placeholder id AND its linkage_key
    name: str             # the callee's simple name
    qualified: str        # <owner fqcn>#<member>, as a consumer would name it
    origin: str           # "stdlib" | "external"
    receiver_type: str | None  # the receiver's simple type name, when known (kept on the edge)


@dataclass
class _Entry:
    type_id: str
    kind: str  # "Class" | "Interface" | "Enum"
    name: str
    package: str
    raw_type: RawType
    file_id: str
    parent_type_id: str | None
    scope_id: str
    scope_label: str | None  # "Package" | "Module" (top-level only; None if nested)
    module: str = ""  # the declaring file's Maven/Gradle module -- biases same-module resolution


@dataclass
class _Callable:
    id: str
    label: str  # "Function" (top-level) | "Method" (member / constructor)
    owner_type_id: str | None  # method's owning type id; None for a top-level function
    owner_kind: str | None     # method's owning type kind (HAS_METHOD source label); None otherwise
    scope_id: str              # for a function: its module/package scope; for a method: same
    scope_label: str | None    # "Package" | "Module" for a function's HAS_FUNCTION source
    package: str
    file_id: str
    raw: RawFunction
    owner_fqcn: str | None  # owning type's fqcn (method) -- the linkage-key owner; None for a function
    module: str = ""  # the declaring file's Maven/Gradle module -- biases same-module resolution


def resolve(raw_files: list[RawFile], project_id: str) -> Iterator[GraphNode | GraphEdge]:
    file_ids: dict[str, str] = {}
    imports_by_file: dict[str, dict[str, str]] = {}  # file_id -> {simple type name -> fqcn}
    modules_by_path: dict[str, str] = {}  # rf.path -> Maven/Gradle module ("" for a root-level file)
    modules_seen: set[str] = set()
    module_packages: set[tuple[str, str]] = set()  # (module, package): package prefix chains per module
    for rf in raw_files:
        fid = ids.file_id(project_id, rf.path)
        file_ids[rf.path] = fid
        imports_by_file[fid] = _import_index(rf)
        module = kotlinmod.module_for_path(rf.path)
        modules_by_path[rf.path] = module
        modules_seen.add(module)
        yield GraphNode("File", {"id": fid}, {"name": _basename(rf.path), "path": _norm(rf.path)})
        yield GraphEdge("HAS_FILE", "Project", {"id": project_id}, "File", {"id": fid})
        if rf.package:
            module_packages.add((module, rf.package))
    yield from _emit_modules(modules_seen, project_id)
    yield from _emit_packages(module_packages, project_id)

    entries: list[_Entry] = []
    for rf in raw_files:
        module = modules_by_path[rf.path]
        scope_root = _scope_root(project_id, module)
        for rt in rf.types:
            _flatten(rt, file_ids[rf.path], scope_root, None, rf.package, entries, module)

    # top-level functions/properties, keyed by their module/package scope.
    top_funcs: list[tuple[str, str, RawFunction, str, str, str]] = []  # (scope_id, scope_label, fn, file_id, package, module)
    top_props: list[tuple[str, str, object, str, str]] = []            # (scope_id, scope_label, RawField, package, module)
    for rf in raw_files:
        module = modules_by_path[rf.path]
        scope_id, scope_label = _top_scope(project_id, module, rf.package)
        for fn in rf.functions:
            top_funcs.append((scope_id, scope_label, fn, file_ids[rf.path], rf.package, module))
        for pr in rf.properties:
            top_props.append((scope_id, scope_label, pr, rf.package, module))

    type_index: dict[str, list[_Entry]] = defaultdict(list)
    for e in entries:
        type_index[e.name].append(e)
    type_by_id = {e.type_id: e for e in entries}
    type_fields = {e.type_id: {f.name: f.type for f in e.raw_type.fields if f.type} for e in entries}

    callables: list[_Callable] = []
    method_index: dict[str, list[tuple[str, str, str, str]]] = defaultdict(list)  # name -> (owner_id, mid, package, module)
    function_index: dict[str, list[tuple[str, str, str, str]]] = defaultdict(list)  # name -> (scope_id, fnid, package, module)

    for e in entries:
        for m in e.raw_type.methods:
            mid = _method_id(e.type_id, m)
            callables.append(_Callable(mid, "Method", e.type_id, e.kind, e.scope_id, None, e.package,
                                       e.file_id, m, _type_fqcn(e), module=e.module))
            method_index[m.name].append((e.type_id, mid, e.package, e.module))
    for scope_id, scope_label, fn, fid, package, module in top_funcs:
        fnid = ids.function_id(scope_id, fn.name, [p.type for p in fn.parameters])
        callables.append(_Callable(fnid, "Function", None, None, scope_id, scope_label, package, fid, fn, None, module=module))
        # (scope_id, fnid, package, module) -- same leading (owner, id, package) shape as method_index so `_pick` works.
        function_index[fn.name].append((scope_id, fnid, package, module))

    # Pass 1: nodes + structural edges (so pass 2's cross-refs always have a landing node).
    for e in entries:
        yield from _emit_type_and_members(e)
    for cal in callables:
        yield from _emit_callable(cal, _real_linkage(cal))
    for scope_id, scope_label, pr, package, module in top_props:
        yield from _emit_top_property(scope_id, scope_label, pr)

    # Pass 2: cross-references. `emitted_externs` dedups placeholder nodes across the whole pass so N
    # callers of one stdlib/other-repo callable MERGE onto a single node (ADR 0004).
    emitted_externs: set[str] = set()
    for e in entries:
        yield from _emit_hierarchy(e, type_index)
        for f in e.raw_type.fields:
            yield from _depends_on("Field", ids.field_id(e.type_id, f.name), [f.type], type_index,
                                   e.package, e.module, e.type_id)
    for cal in callables:
        member_types: dict[str, str] = {}
        if cal.owner_type_id:
            member_types.update(type_fields.get(cal.owner_type_id, {}))
        member_types.update({p.name: p.type for p in cal.raw.parameters if p.type})
        yield from _emit_calls(cal, member_types, imports_by_file.get(cal.file_id, {}),
                               type_index, method_index, function_index, type_by_id, emitted_externs)
        type_texts = [cal.raw.return_type, *(p.type for p in cal.raw.parameters)]
        yield from _depends_on(cal.label, cal.id, type_texts, type_index, cal.package, cal.module, cal.owner_type_id)
    for scope_id, scope_label, pr, package, module in top_props:
        yield from _depends_on("Field", ids.field_id(scope_id, pr.name), [pr.type], type_index, package, module, None)


# -- modules & packages --------------------------------------------------------------------

def _scope_root(project_id: str, module: str) -> str:
    """The id a file's packages/types nest under: the Module node for a real Maven/Gradle subproject,
    or the Project itself for a root-level file (`module == ""`). Keeping root files Project-scoped
    means a root file and a subproject literally named after the project never collapse to one id, and
    the Module tier appears only when there genuinely are subprojects."""
    return ids.module_id(project_id, module) if module else project_id


def _emit_modules(modules: set[str], project_id: str) -> Iterator[GraphNode | GraphEdge]:
    """Materialize the Maven/Gradle module tier: Project -[:HAS_MODULE]-> Module, one per subproject
    (path-derived, see kotlinmod). The module is the scope root the package tier nests under, so two
    modules' identically-named packages/types stay distinct. Root-level files (`module == ""`) get NO
    Module node -- they hang off the Project directly (see _scope_root). A Kotlin module carries NO
    linkage_key: a consumer imports by FQCN, not module name, so the module heals nothing (cross-repo
    linkage stays FQCN-based). Kotlin has no delta-sync, so (unlike Java) no File-DEFINES-Module
    provenance is emitted -- the whole subgraph is rebuilt each ingest."""
    for module in sorted(modules):
        if not module:
            continue  # root-level file -> Project-scoped, no Module node
        mid = ids.module_id(project_id, module)
        yield GraphNode("Module", {"id": mid}, {"name": module})
        yield GraphEdge("HAS_MODULE", "Project", {"id": project_id}, "Module", {"id": mid})


def _emit_packages(module_packages: set[tuple[str, str]], project_id: str) -> Iterator[GraphNode | GraphEdge]:
    """Materialize every prefix of every declared package as a Package node (as Java does), nested
    under its scope root: the first segment hangs off the file's Module (a subproject) or the Project
    (a root-level file) via HAS_PACKAGE, deeper segments chain via HAS_SUBPACKAGE -- all keyed under
    that scope root so two modules' `com.acme` stay distinct."""
    emitted: set[str] = set()
    for module, package in module_packages:
        scope_root = _scope_root(project_id, module)
        prefix = ""
        parent_id, parent_label, parent_rel = scope_root, ("Module" if module else "Project"), "HAS_PACKAGE"
        for segment in package.split("."):
            prefix = f"{prefix}.{segment}" if prefix else segment
            pid = ids.package_id(scope_root, prefix)
            if pid not in emitted:
                emitted.add(pid)
                yield GraphNode("Package", {"id": pid}, {"name": segment, "qualified_name": prefix})
                yield GraphEdge(parent_rel, parent_label, {"id": parent_id}, "Package", {"id": pid})
            parent_id, parent_label, parent_rel = pid, "Package", "HAS_SUBPACKAGE"


# -- flattening ----------------------------------------------------------------------------

def _flatten(rt: RawType, file_id: str, scope_root: str, parent_type_id: str | None, package: str,
             entries: list[_Entry], module: str) -> None:
    """`scope_root` is the file's per-file scope root (its Module for a subproject, else the Project --
    see _scope_root), which the package tier and default-package types nest under, so two modules'
    identically-named packages/types stay distinct."""
    if parent_type_id is not None:
        tid = ids.type_id(parent_type_id, rt.kind, rt.name, rt.type_parameters)
        scope_id, scope_label = parent_type_id, None
    elif package:
        scope_id = ids.package_id(scope_root, package)
        scope_label = "Package"
        tid = ids.type_id(scope_id, rt.kind, rt.name, rt.type_parameters)
    else:
        # default-package types hang off the scope root: the Module for a subproject, else the Project.
        scope_id, scope_label = scope_root, ("Module" if module else "Project")
        tid = ids.type_id(scope_id, rt.kind, rt.name, rt.type_parameters)

    entries.append(_Entry(tid, rt.kind, rt.name, package, rt, file_id, parent_type_id, scope_id, scope_label, module=module))
    for nested in rt.nested_types:
        _flatten(nested, file_id, scope_root, tid, package, entries, module)


def _top_scope(project_id: str, module: str, package: str) -> tuple[str, str]:
    scope_root = _scope_root(project_id, module)
    if package:
        return ids.package_id(scope_root, package), "Package"
    return scope_root, ("Module" if module else "Project")


# -- pass 1: nodes + structural edges ------------------------------------------------------

def _type_properties(rt: RawType) -> dict:
    props: dict = {
        "name": rt.name,
        "qualified_name": f"{rt.package}.{rt.name}" if rt.package else rt.name,
        "accessibility": rt.accessibility,
        "type_parameters": list(rt.type_parameters),
        "annotations": [a.name for a in rt.annotations],
        "source": rt.source,
        "doc": rt.doc,
        "line_start": rt.line_start,
        "line_end": rt.line_end,
    }
    if rt.kind == "Class":
        props.update(
            is_abstract="abstract" in rt.modifiers, is_open="open" in rt.modifiers,
            is_sealed="sealed" in rt.modifiers, is_data=rt.is_data, is_object=rt.is_object,
            is_companion=rt.is_companion,
        )
    return props


_KIND_BY_SLUG = {"class": "Class", "interface": "Interface", "enum": "Enum"}


def _parent_kind(e: _Entry) -> str:
    tail = e.parent_type_id.rsplit(".", 2)[-2] if e.parent_type_id else "class"
    return _KIND_BY_SLUG.get(tail, "Class")


def _emit_type_and_members(e: _Entry) -> Iterator[GraphNode | GraphEdge]:
    yield GraphNode(e.kind, {"id": e.type_id}, _type_properties(e.raw_type))
    if e.parent_type_id is not None:
        yield GraphEdge("HAS_NESTED_TYPE", _parent_kind(e), {"id": e.parent_type_id}, e.kind, {"id": e.type_id})
    else:
        yield GraphEdge("HAS_TYPE", e.scope_label, {"id": e.scope_id}, e.kind, {"id": e.type_id})
    yield GraphEdge("DEFINED_IN", e.kind, {"id": e.type_id}, "File", {"id": e.file_id})

    for f in e.raw_type.fields:
        yield from _emit_field(e.type_id, e.kind, f)
    for member in e.raw_type.enum_members:
        mid = ids.enum_member_id(e.type_id, member.name)
        yield GraphNode("EnumMember", {"id": mid}, {"name": member.name, "arguments": member.arguments})
        yield GraphEdge("HAS_ENUM_MEMBER", "Enum", {"id": e.type_id}, "EnumMember", {"id": mid})


def _method_id(type_id: str, m: RawFunction) -> str:
    return ids.method_id(type_id, m.name, [p.type for p in m.parameters])


def _emit_callable(cal: _Callable, linkage: str | None) -> Iterator[GraphNode | GraphEdge]:
    fn = cal.raw
    props = {
        "name": fn.name,
        "accessibility": fn.accessibility,
        "modifiers": list(fn.modifiers),
        "is_abstract": "abstract" in fn.modifiers,
        "is_open": "open" in fn.modifiers,
        "is_override": "override" in fn.modifiers,
        "is_suspend": "suspend" in fn.modifiers,
        "return_type": fn.return_type,
        "annotations": [a.name for a in fn.annotations],
        "source": fn.source,
        "doc": fn.doc,
        "line_start": fn.line_start,
        "line_end": fn.line_end,
    }
    if linkage is not None:  # ADR 0004: an externally-visible callable carries a cross-repo key
        props["linkage_key"] = linkage
    if cal.label == "Method":
        props["kind"] = fn.kind  # "ORDINARY" | "CONSTRUCTOR"
        yield GraphNode("Method", {"id": cal.id}, props)
        yield GraphEdge("HAS_METHOD", cal.owner_kind, {"id": cal.owner_type_id}, "Method", {"id": cal.id})
    else:
        yield GraphNode("Function", {"id": cal.id}, props)
        yield GraphEdge("HAS_FUNCTION", cal.scope_label, {"id": cal.scope_id}, "Function", {"id": cal.id})
    for i, p in enumerate(fn.parameters):
        yield from _emit_parameter(cal.id, cal.label, p, i)


def _emit_parameter(parent_id: str, parent_label: str, param, position: int) -> Iterator[GraphNode | GraphEdge]:
    pid = ids.parameter_id(parent_id, position, param.name)
    yield GraphNode("Parameter", {"id": pid}, {
        "name": param.name, "type": param.type, "position": param.position,
        "is_vararg": param.is_vararg, "has_default": param.has_default,
    })
    yield GraphEdge("HAS_PARAMETER", parent_label, {"id": parent_id}, "Parameter", {"id": pid})


def _emit_field(owner_id: str, owner_label: str, f) -> Iterator[GraphNode | GraphEdge]:
    fid = ids.field_id(owner_id, f.name)
    yield GraphNode("Field", {"id": fid}, {
        "name": f.name, "type": f.type, "accessibility": f.accessibility,
        "is_mutable": f.is_mutable, "annotations": [a.name for a in f.annotations],
    })
    yield GraphEdge("HAS_FIELD", owner_label, {"id": owner_id}, "Field", {"id": fid})


def _emit_top_property(scope_id: str, scope_label: str, f) -> Iterator[GraphNode | GraphEdge]:
    yield from _emit_field(scope_id, scope_label, f)


# -- pass 2: hierarchy, type references, calls ---------------------------------------------

def _resolve_target(name: str | None, type_index: dict[str, list[_Entry]], from_package: str, from_module: str) -> _Entry | None:
    if not name:
        return None
    matches = type_index.get(name)
    if not matches:
        return None
    # Bias toward the referrer's own module/package: within one build only the caller's module (plus
    # its deps) is on the classpath, so two modules' same-package `Helper` must not cross-bind. Prefer
    # same-module+same-package, then same-package, then same-module, then anywhere (deterministic).
    same_mod_pkg = [m for m in matches if m.module == from_module and m.package == from_package]
    same_pkg = [m for m in matches if m.package == from_package]
    same_mod = [m for m in matches if m.module == from_module]
    pool = same_mod_pkg or same_pkg or same_mod or matches
    return sorted(pool, key=lambda m: m.type_id)[0]  # deterministic on ambiguity


def _emit_hierarchy(e: _Entry, type_index: dict[str, list[_Entry]]) -> Iterator[GraphEdge]:
    rt = e.raw_type
    if rt.superclass:  # class/object -> class
        target = _resolve_target(rt.superclass, type_index, e.package, e.module)
        if target is not None and target.type_id != e.type_id:
            yield GraphEdge("EXTENDS", e.kind, {"id": e.type_id}, target.kind, {"id": target.type_id})
    for iface in rt.interfaces:
        target = _resolve_target(iface, type_index, e.package, e.module)
        if target is None or target.type_id == e.type_id:
            continue
        # an interface's supertypes are super-interfaces -> EXTENDS; a class/enum/object -> IMPLEMENTS.
        rel = "EXTENDS" if e.kind == "Interface" else "IMPLEMENTS"
        yield GraphEdge(rel, e.kind, {"id": e.type_id}, target.kind, {"id": target.type_id})


def _referenced_names(type_text: str | None) -> list[str]:
    if not type_text:
        return []
    return [n for n in _IDENTIFIER_RE.findall(type_text) if n not in _NON_TYPE_TOKENS]


def _depends_on(from_label: str, from_id: str, type_texts, type_index, from_package: str,
                from_module: str, skip_type_id: str | None) -> Iterator[GraphEdge]:
    names: set[str] = set()
    for tt in type_texts:
        names.update(_referenced_names(tt))
    emitted: set[str] = set()
    for name in names:
        target = _resolve_target(name, type_index, from_package, from_module)
        if target is None or target.type_id == skip_type_id or target.type_id in emitted:
            continue
        emitted.add(target.type_id)
        yield GraphEdge("DEPENDS_ON", from_label, {"id": from_id}, target.kind, {"id": target.type_id})


def _pick(candidates: list[tuple[str, str, str, str]]) -> str:
    return sorted(candidates, key=lambda c: c[1])[0][1]


def _pick_scoped(candidates: list[tuple[str, str, str, str]], cal: _Callable) -> str:
    """Module/package-biased pick for method/function candidates `(owner_or_scope_id, id, package,
    module)`: a candidate from the referrer's own module+package wins, then same-package, then
    same-module, then anywhere -- keeps two modules' same-named, same-package callables from
    cross-binding (the callable analog of _resolve_target's bias)."""
    same_mod_pkg = [c for c in candidates if c[3] == cal.module and c[2] == cal.package]
    same_pkg = [c for c in candidates if c[2] == cal.package]
    same_mod = [c for c in candidates if c[3] == cal.module]
    return _pick(same_mod_pkg or same_pkg or same_mod or candidates)


def _resolve_call(call, cal: _Callable, member_types: dict[str, str], imports: dict[str, str],
                  type_index, method_index, function_index):
    """Resolve one call to `("local", target_id, target_label, extra)`, `("extern", _Extern)`, or
    None (an unnameable call, or an intra-project dangling reference deliberately left dropped so an
    incremental resync leaves no stale `extern://` behind)."""
    name = call.name
    recv = call.receiver

    if recv is None:
        # (1) receiver-less call naming a project type -> that type's constructor (Kotlin has no `new`).
        target = _resolve_target(name, type_index, cal.package, cal.module)
        if target is not None:
            ctor = next((m for m in target.raw_type.methods if m.kind == "CONSTRUCTOR"), None)
            if ctor is not None:
                return "local", _method_id(target.type_id, ctor), "Method", {}
            return None  # a project type with only an implicit constructor -> intra-project, drop
        # (2) a top-level function (function_index tuples are (scope_id, fnid, package, module)).
        fcands = function_index.get(name)
        if fcands:
            same_scope = [c for c in fcands if c[0] == cal.scope_id]  # exact module+package scope
            return "local", _pick(same_scope) if same_scope else _pick_scoped(fcands, cal), "Function", {}
        # (3) a member method reached without a receiver (implicit `this`).
        mcands = method_index.get(name)
        if mcands:
            same_type = [c for c in mcands if c[0] == cal.owner_type_id]  # same-module (type_id is module-scoped)
            if same_type:
                return "local", _pick(same_type), "Method", {}
            return "local", _pick_scoped(mcands, cal), "Method", {}
        # (4) a capitalized bare call is a constructor of a non-project type -> external.
        if name[:1].isupper():
            return _extern_result(name, name, imports, receiver_type=None)
        return None  # a bare lowercase call we can't name -> drop

    if recv == "this":
        mcands = [c for c in method_index.get(name, []) if c[0] == cal.owner_type_id]
        if mcands:
            return "local", _pick(mcands), "Method", {}
        return None  # a missing/inherited member on our own type -> intra-project, drop

    # receiver is a bare identifier: prefer a field/param's declared type, then a type (static/companion).
    rtype = member_types.get(recv)
    if rtype is not None:
        resolved_project = False
        for type_name in _referenced_names(rtype):
            target_type = _resolve_target(type_name, type_index, cal.package, cal.module)
            if target_type is None:
                continue
            resolved_project = True
            matches = [c for c in method_index.get(name, []) if c[0] == target_type.type_id]
            if matches:
                return "local", _pick(matches), "Method", {"receiver_type": target_type.name}
        if resolved_project:
            return None  # receiver is a project type, but missing this member -> intra-project, drop
        head = next(iter(_referenced_names(rtype)), None)
        return _extern_result(head, name, imports, receiver_type=head) if head else None

    target_type = _resolve_target(recv, type_index, cal.package, cal.module)  # a `Type.method()` / companion call
    if target_type is not None:
        matches = [c for c in method_index.get(name, []) if c[0] == target_type.type_id]
        if matches:
            return "local", _pick(matches), "Method", {"receiver_type": target_type.name}
        return None  # static/companion call on a PROJECT type missing the member -> intra-project, drop
    if recv in imports or recv[:1].isupper():
        return _extern_result(recv, name, imports, receiver_type=recv)
    return None  # a local variable of unknown type -- can't name the owning type


def _extern_result(type_simple: str, member: str, imports: dict[str, str], receiver_type: str | None):
    """Build the `("extern", _Extern)` result for a call on `type_simple.member` -- resolving the
    type's fqcn from this file's imports/builtins so the linkage key matches the real definition's."""
    fqcn = _fqcn(type_simple, imports)
    qualified = f"{fqcn}#{member}"
    return "extern", _Extern(
        linkage=ids.extern_method_key(qualified), name=member, qualified=qualified,
        origin=_origin(fqcn), receiver_type=receiver_type,
    )


def _emit_calls(cal: _Callable, member_types: dict[str, str], imports: dict[str, str],
                type_index, method_index, function_index, type_by_id, emitted_externs: set[str]
                ) -> Iterator[GraphNode | GraphEdge]:
    emitted: set[str] = set()  # dedup CALLS targets from this callable (project id or placeholder id)
    for call in cal.raw.calls:
        hit = _resolve_call(call, cal, member_types, imports, type_index, method_index, function_index)
        if hit is None:
            continue
        if hit[0] == "local":
            _, target_id, target_label, extra = hit
            if target_id == cal.id or target_id in emitted:
                continue
            emitted.add(target_id)
            yield GraphEdge("CALLS", cal.label, {"id": cal.id}, target_label, {"id": target_id},
                            {"resolution": "name", **extra})
        else:  # ("extern", _Extern): a placeholder Method reached by an ordinary CALLS edge (ADR 0004)
            ext = hit[1]
            if ext.linkage in emitted:
                continue
            emitted.add(ext.linkage)
            if ext.linkage not in emitted_externs:
                emitted_externs.add(ext.linkage)
                yield _external_method_node(ext)
            props = {"resolution": "external"}
            if ext.receiver_type is not None:
                props["receiver_type"] = ext.receiver_type
            yield GraphEdge("CALLS", cal.label, {"id": cal.id}, "Method", {"id": ext.linkage}, props)


# -- helpers -------------------------------------------------------------------------------

def _real_linkage(cal: _Callable) -> str | None:
    """ADR 0004 cross-repo `linkage_key` for a real callable, or None for one not visible outside its
    module (private/internal). Name-based, in the form a consumer writes: a method -> `<owner
    fqcn>#<member>` (a constructor's member is the type's simple name); a top-level function ->
    its package-qualified name (`com.acme.util.helper`)."""
    if cal.raw.accessibility not in ("public", "protected"):
        return None
    if cal.label == "Method":
        return ids.extern_method_key(f"{cal.owner_fqcn}#{cal.raw.name}")
    qualified = f"{cal.package}.{cal.raw.name}" if cal.package else cal.raw.name
    return ids.extern_method_key(qualified)


def _import_index(rf: RawFile) -> dict[str, str]:
    """Map a simple type name (or import alias) to its fqcn from this file's non-wildcard imports, so
    a call on an external/JDK receiver can be named by its real fqcn (ADR 0004)."""
    idx: dict[str, str] = {}
    for imp in rf.imports:
        if imp.is_wildcard:
            continue
        simple = imp.alias or imp.name.rsplit(".", 1)[-1]
        idx.setdefault(simple, imp.name)
    return idx


def _fqcn(simple: str, imports: dict[str, str]) -> str:
    """Resolve a simple type name to its fully-qualified name via this file's imports, falling back to
    the Kotlin auto-imported builtins, else the bare simple name (still a stable linkage key)."""
    if simple in imports:
        return imports[simple]
    if simple in _KOTLIN_BUILTINS:
        return _KOTLIN_BUILTINS[simple]
    return simple


def _type_fqcn(e: _Entry) -> str:
    return f"{e.package}.{e.name}" if e.package else e.name


def _external_method_node(ext: _Extern) -> GraphNode:
    """A placeholder for an unresolved call, keyed by its cross-repo linkage so a later ingest of the
    defining repo takes it over (ADR 0004). Shallow: shape mirrors a real Method so the same CALLS
    queries reach it; `resolved:false` + `origin` mark it."""
    return GraphNode("Method", {"id": ext.linkage}, {
        "name": ext.name, "qualified_name": ext.qualified, "resolved": False,
        "origin": ext.origin, "linkage_key": ext.linkage,
    })


def _norm(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def _basename(path: str) -> str:
    return _norm(path).rsplit("/", 1)[-1]
