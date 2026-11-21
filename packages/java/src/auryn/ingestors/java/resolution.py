"""Project-wide pass (phase 2): turn the per-file RawFile list into graph nodes/edges, applying
the cross-file decisions a single file can't -- materializing the package tier, resolving
EXTENDS/IMPLEMENTS and parameter/return/field type references to project types, and best-effort
call resolution.

Adapted from the C# ingestor's resolution (Java is its structural sibling), pared to Java: the
tier is `Package`; EXTENDS vs IMPLEMENTS needs no naming heuristic because Java exposes the
single `superclass` separately from the `interfaces` list; and the C# DTO/APIEndpoint layer is
dropped (a Spring enrichment is the future analog).

Name-based only -- this is the fast tree-sitter engine, so every CALLS edge is stamped
`resolution="name"`. A future deep JDT/javac pass would re-emit the same node ids with a
stronger provenance (the two-phase model). Ambiguous or unresolved references produce no edge
rather than a wrong guess -- the same trade-off as the C#/PL/SQL ingestors."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field

from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.ingestors.java import ids, javamod
from auryn.ingestors.java.raw_model import RawCall, RawFile, RawMethod, RawType

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Type tokens that are never a project type -- Java primitives/keywords appearing in type text.
_NON_TYPE_TOKENS = frozenset(
    {"void", "int", "long", "short", "byte", "char", "boolean", "float", "double",
     "var", "extends", "super"}
)

# ADR 0004: the common `java.lang` types that are implicitly imported (never written as an
# `import`), so a call on one can still be classified `stdlib` and keyed by its real fqcn even
# without an import to resolve the simple name. Not exhaustive -- the surface that shows up as a
# call receiver / `new` target in practice.
_JAVA_LANG = frozenset({
    "Object", "String", "StringBuilder", "StringBuffer", "CharSequence",
    "Integer", "Long", "Short", "Byte", "Character", "Boolean", "Float", "Double", "Number",
    "Math", "System", "Thread", "Runnable", "Throwable", "Exception", "RuntimeException",
    "Error", "Class", "Enum", "Iterable", "Comparable", "Void",
})


def _origin(fqcn: str) -> str:
    """Coarse provenance for a placeholder's callee (ADR 0004 §4): a JDK/stdlib symbol (never
    upgraded by a future ingest) vs an `external` one (the candidate a sibling repo defines)."""
    return "stdlib" if fqcn.startswith(("java.", "javax.", "jdk.")) else "external"


@dataclass
class _Extern:
    """A callee the resolver could NAME but that is not defined in this ingest -- a JDK/stdlib or
    other-repo method. Becomes a placeholder Method keyed by its cross-repo linkage (ADR 0004)."""
    linkage: str          # extern://java/<qualified> -- the placeholder id AND its linkage_key
    name: str             # the callee's simple name
    qualified: str        # <owner fqcn>#<member>, as a consumer would name it
    origin: str           # "stdlib" | "external"
    receiver_type: str | None  # the receiver's simple type name, when known (kept on the edge)


@dataclass
class _Entry:
    type_id: str
    kind: str  # "Class" | "Interface" | "Enum" | "Record"
    name: str
    package: str
    raw_type: RawType
    file_id: str
    parent_type_id: str | None
    scope_id: str
    scope_label: str | None  # "Package" | "Module" (top-level only; None if nested)
    module: str = ""  # the declaring file's Maven/Gradle module -- biases same-module resolution
    seed_ctor_id: str | None = None  # a NON-emit (seed) type's constructor id, for `new` resolution


@dataclass
class JavaGraphIndex:
    """Java resolution tables seeded from the GRAPH for the NON-emit files (ADR 0005), so
    `resolve_incremental` can resolve an emit file's cross-file/cross-package refs without re-parsing
    the project. type_index carries lightweight `_Entry` stubs (empty raw_type; `seed_ctor_id` lets a
    `new` target resolve its constructor); method_index/packages mirror what the full resolve builds
    from entries. Reconstructed by `java.incremental.read_java_index`."""

    packages: set = field(default_factory=set)
    type_index: dict = field(default_factory=dict)     # name -> [_Entry stub]
    method_index: dict = field(default_factory=dict)   # name -> [(type_id, method_id, package, module)]


def make_type_stub(type_id: str, kind: str, name: str, package: str, module: str, ctor_id: str | None) -> _Entry:
    """A lightweight `_Entry` for a NON-emit type the seed provides: only id/kind/name/package/module
    are needed to point a resolved edge at it (module biases same-module resolution); `ctor_id` (its
    graph constructor, if any) lets an emit file's `new` resolve without the raw type body."""
    return _Entry(type_id, kind, name, package, RawType(kind=kind, name=name, package=package),
                  file_id="", parent_type_id=None, scope_id="", scope_label=None, module=module, seed_ctor_id=ctor_id)


def resolve(raw_files: list[RawFile], project_id: str) -> Iterator[GraphNode | GraphEdge]:
    yield from _resolve(raw_files, project_id, seed=None)


def resolve_incremental(emit_raws: list[RawFile], project_id: str, index: JavaGraphIndex) -> list:
    """Emit graph items for ONLY `emit_raws`, resolving cross-file/cross-package refs against `index`
    (graph-seeded non-emit symbols) merged with the emit files' own. The blast-radius analogue of
    resolve() (ADR 0005): same emitters, indices seeded rather than whole-project."""
    return list(_resolve(emit_raws, project_id, seed=index))


def callable_locations(raw_files: list[RawFile], project_id: str):
    """Every Java method/constructor's SCIP-join location (graph id, file, 1-based line span, name, called
    names) -- the bridge the deep (scip-java / SCIP) pass needs. Reuses the fast pass's OWN flattening
    (_flatten -> _Entry, keyed via the module->package->type scope in _scope_root/ids) and _method_id, so
    deep CALLS edges land byte-identically on the Method nodes resolve() authored -- no type inference.
    A constructor's location name is `<init>` (scip-java's JVM name for it), while its id stays
    class-name-based, so a resolved `new X()` deep edge still lands on the right node. A parity test
    asserts these ids subset resolve()'s Method node ids. The core.scip_resolve import is deferred here
    (deep-only) so the fast pass never requires it (buildable against an older auryn-core)."""
    from auryn.core.scip_resolve import CallableLocation

    raw_files = sorted(raw_files, key=lambda rf: _norm(rf.path))
    path_by_fid: dict[str, str] = {}
    entries: list[_Entry] = []
    for rf in raw_files:
        fid = ids.file_id(project_id, rf.path)
        path_by_fid[fid] = _norm(rf.path)
        module = javamod.module_for_path(rf.path)
        scope_root = _scope_root(project_id, module)
        for rt in rf.types:
            _flatten(rt, fid, scope_root, None, rf.package, entries, module)
    seen_tids: set[str] = set()  # keep-first dedup, matching _resolve
    entries = [e for e in entries if not (e.type_id in seen_tids or seen_tids.add(e.type_id))]

    out = []
    for e in entries:
        rel = path_by_fid.get(e.file_id, "")
        for m in e.raw_type.methods:
            name = "<init>" if m.kind == "CONSTRUCTOR" else m.name
            out.append(CallableLocation(_method_id(e.type_id, m), "Method", rel,
                                        m.line_start, m.line_end, name,
                                        frozenset(c.name for c in m.calls)))
    return out


def _resolve(raw_files: list[RawFile], project_id: str, seed: "JavaGraphIndex | None") -> Iterator[GraphNode | GraphEdge]:
    # Stable path order so a symbol somehow defined in several files reduces to an order-INDEPENDENT
    # winner -- identical here and in resolve_incremental's subset (ADR 0005). Java forbids duplicate
    # FQNs so this is belt-and-braces, but keeps the invariant the engine relies on.
    raw_files = sorted(raw_files, key=lambda rf: _norm(rf.path))

    file_ids: dict[str, str] = {}
    imports_by_file: dict[str, dict[str, str]] = {}  # file_id -> {simple type name -> fqcn}
    modules_by_path: dict[str, str] = {}  # rf.path -> Maven/Gradle module (path-derived scope root)
    file_modules: list[tuple[str, str]] = []  # (file_id, module): File-DEFINES-Module provenance
    file_packages: list[tuple[str, str, str]] = []  # (file_id, module, package): File-DEFINES-Package provenance
    for rf in raw_files:
        fid = ids.file_id(project_id, rf.path)
        file_ids[rf.path] = fid
        imports_by_file[fid] = _import_index(rf)
        module = javamod.module_for_path(rf.path)
        modules_by_path[rf.path] = module
        file_modules.append((fid, module))
        yield GraphNode("File", {"id": fid}, {"name": _basename(rf.path), "path": _norm(rf.path)})
        yield GraphEdge("HAS_FILE", "Project", {"id": project_id}, "File", {"id": fid})
        if rf.package:
            file_packages.append((fid, module, rf.package))
    yield from _emit_modules(file_modules, project_id)
    yield from _emit_packages(file_packages, project_id)

    emit_entries: list[_Entry] = []
    for rf in raw_files:
        module = modules_by_path[rf.path]
        scope_root = _scope_root(project_id, module)
        for rt in rf.types:
            _flatten(rt, file_ids[rf.path], scope_root, None, rf.package, emit_entries, module)
    # Keep-first dedup by type_id (deterministic winner if an id ever repeats) -- the emitter must
    # emit each node id at most once (the engine's read-back/diff assumes it).
    seen_tids: set[str] = set()
    emit_entries = [e for e in emit_entries if not (e.type_id in seen_tids or seen_tids.add(e.type_id))]

    # Resolution indices span emit entries + the graph-seeded non-emit symbols, so an emit file's
    # cross-file/cross-package reference still resolves to an untouched target.
    type_index: dict[str, list[_Entry]] = defaultdict(list)
    for e in emit_entries:
        type_index[e.name].append(e)
    method_index: dict[str, list[tuple[str, str, str, str]]] = defaultdict(list)
    for e in emit_entries:
        for m in e.raw_type.methods:
            method_index[m.name].append((e.type_id, _method_id(e.type_id, m), e.package, e.module))
    if seed is not None:
        for name, stubs in seed.type_index.items():
            type_index[name].extend(stubs)
        for name, tuples in seed.method_index.items():
            method_index[name].extend(tuples)

    # Pass 1: nodes + structural edges (so pass 2's cross-refs always have a landing node). EMIT ONLY
    # the emit entries; seed entries exist purely as resolution targets.
    for e in emit_entries:
        yield from _emit_type_and_members(e)
    # Pass 2: cross-references. `emitted_externs` dedups placeholder nodes across the whole pass
    # so N callers of one JDK/other-repo method MERGE onto a single node (ADR 0004).
    emitted_externs: set[str] = set()
    for e in emit_entries:
        yield from _emit_hierarchy(e, type_index)
        yield from _emit_type_references(e, type_index)
        yield from _emit_calls(e, type_index, method_index, imports_by_file.get(e.file_id, {}), emitted_externs)


# -- modules & packages --------------------------------------------------------------------

def _scope_root(project_id: str, module: str) -> str:
    """The id a file's packages/types nest under: the Module node for a real Maven/Gradle subproject,
    or the Project itself for a root-level file (`module == ""`). Keeping root files Project-scoped
    (rather than under a Module named after the project) means a root file and a subproject literally
    named after the project -- both `src/main/java/...` and `<proj>/src/main/java/...` -- can never
    collapse to one id. The Module tier therefore appears only when there genuinely ARE subprojects."""
    return ids.module_id(project_id, module) if module else project_id


def _emit_modules(file_modules: list[tuple[str, str]], project_id: str) -> Iterator[GraphNode | GraphEdge]:
    """Materialize the Maven/Gradle module tier: Project -[:HAS_MODULE]-> Module, one per subproject
    (path-derived, see javamod). The module is the scope root the package tier nests under, so two
    modules' identically-named packages/types stay distinct. Root-level files (`module == ""`) get NO
    Module node -- they hang off the Project directly (see _scope_root). Like Package, a Module is an
    aggregation node many files feed, so it is provenance-owned (File-[:DEFINES]->Module) and reclaimed
    once its last member file is gone (ADR 0005), and excluded from the co-DEFINER winner closure
    (JAVA_SPEC.aggregation_labels). A Java module carries NO linkage_key: a consumer imports by FQCN,
    not by module name, so the module heals nothing (cross-repo linkage stays FQCN-based)."""
    emitted: set[str] = set()
    def_edges: set[tuple[str, str]] = set()  # (file_id, module_id), dedup exact repeats
    for file_id, module in file_modules:
        if not module:
            continue  # root-level file -> Project-scoped, no Module node
        mid = ids.module_id(project_id, module)
        if mid not in emitted:
            emitted.add(mid)
            yield GraphNode("Module", {"id": mid}, {"name": module})
            yield GraphEdge("HAS_MODULE", "Project", {"id": project_id}, "Module", {"id": mid})
        def_edges.add((file_id, mid))
    for file_id, mid in sorted(def_edges):
        yield GraphEdge("DEFINES", "File", {"id": file_id}, "Module", {"id": mid})


def _emit_packages(file_packages: list[tuple[str, str, str]], project_id: str) -> Iterator[GraphNode | GraphEdge]:
    """Materialize every prefix of every declared package as a Package node (as C# does for
    namespaces), nested under its scope root: the first segment hangs off the file's Module (a
    subproject) or the Project (a root-level file) via HAS_PACKAGE, deeper segments chain via
    HAS_SUBPACKAGE. `com.acme.app` -> Package(com) -> Package(com.acme) -> Package(com.acme.app), all
    keyed under that scope root so two modules' `com.acme` stay distinct.

    Also emits a File-[:DEFINES]->Package edge from each declaring file to EVERY prefix of its package
    (ADR 0005 GC handle): a prefix Package is reclaimed once its last member file is gone, exactly like
    a full recompute -- otherwise deleting a package's last file strands the (no-longer-owned) Package
    nodes. Package props are id-derived, so Package is excluded from the co-DEFINER winner closure
    (JAVA_SPEC.aggregation_labels): a shared prefix like `com` is co-declared by every file beneath it,
    which would otherwise drag the whole subtree into any one file's resync."""
    emitted: set[str] = set()
    def_edges: set[tuple[str, str]] = set()  # (file_id, package_id), dedup exact repeats
    for file_id, module, package in file_packages:
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
            def_edges.add((file_id, pid))
            parent_id, parent_label, parent_rel = pid, "Package", "HAS_SUBPACKAGE"
    for file_id, pid in sorted(def_edges):
        yield GraphEdge("DEFINES", "File", {"id": file_id}, "Package", {"id": pid})


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
        props.update(is_abstract="abstract" in rt.modifiers, is_final="final" in rt.modifiers,
                     is_static="static" in rt.modifiers)
    elif rt.kind == "Record":
        props["components"] = [p.name for p in rt.record_components]
    return props


def _emit_type_and_members(e: _Entry) -> Iterator[GraphNode | GraphEdge]:
    # `module` lets read_java_index reconstruct an incremental stub's module for same-module
    # resolution bias without re-parsing (ADR 0005) -- the only non-id-derivable fact a stub needs.
    yield GraphNode(e.kind, {"id": e.type_id}, {**_type_properties(e.raw_type), "module": e.module})
    if e.parent_type_id is not None:
        yield GraphEdge("HAS_NESTED_TYPE", _parent_kind(e), {"id": e.parent_type_id}, e.kind, {"id": e.type_id})
    else:
        yield GraphEdge("HAS_TYPE", e.scope_label, {"id": e.scope_id}, e.kind, {"id": e.type_id})
    yield GraphEdge("DEFINED_IN", e.kind, {"id": e.type_id}, "File", {"id": e.file_id})
    # Per-file ownership handle for delta-sync (File→entity, ADR 0005; uniform with C/Go). A type and
    # its methods are always co-located in Java, so both DEFINES edges source at the same File.
    yield GraphEdge("DEFINES", "File", {"id": e.file_id}, e.kind, {"id": e.type_id})

    owner_fqcn = _type_fqcn(e)
    for m in e.raw_type.methods:
        yield from _emit_method(e.type_id, e.kind, m, owner_fqcn, e.file_id)
    for f in e.raw_type.fields:
        yield from _emit_field(e.type_id, e.kind, f)
    for member in e.raw_type.enum_members:
        mid = ids.enum_member_id(e.type_id, member.name)
        yield GraphNode("EnumMember", {"id": mid}, {"name": member.name, "arguments": member.arguments})
        yield GraphEdge("HAS_ENUM_MEMBER", "Enum", {"id": e.type_id}, "EnumMember", {"id": mid})
    for i, comp in enumerate(e.raw_type.record_components):  # record components -> Parameters
        yield from _emit_parameter(e.type_id, e.kind, comp, i)


# _parent_kind: the parent entry's kind is needed as the HAS_NESTED_TYPE source label. Nested
# types are rare enough that a re-derivation via the id suffix is cheaper than threading the
# parent label through _flatten.
_KIND_BY_SLUG = {"class": "Class", "interface": "Interface", "enum": "Enum", "record": "Record"}


def _parent_kind(e: _Entry) -> str:
    # parent_type_id ends with ".<slug>.<name>[`arity]"; pull the slug.
    tail = e.parent_type_id.rsplit(".", 2)[-2] if e.parent_type_id else "class"
    return _KIND_BY_SLUG.get(tail, "Class")


def _method_id(type_id: str, m: RawMethod) -> str:
    return ids.method_id(type_id, m.name, [p.type for p in m.parameters])


def _emit_method(type_id: str, type_label: str, m: RawMethod, owner_fqcn: str, file_id: str) -> Iterator[GraphNode | GraphEdge]:
    mid = _method_id(type_id, m)
    props = {
        "name": m.name,
        "kind": m.kind,
        "accessibility": m.accessibility,
        "is_static": "static" in m.modifiers,
        "is_abstract": "abstract" in m.modifiers,
        "is_final": "final" in m.modifiers,
        "return_type": m.return_type,
        "throws": m.throws,
        "annotations": [a.name for a in m.annotations],
        "source": m.source,
        "doc": m.doc,
        "line_start": m.line_start,
        "line_end": m.line_end,
    }
    # ADR 0004: an externally-visible method carries a project-INDEPENDENT cross-repo key, so a
    # placeholder emitted by another repo's call heals onto it (reconcile). Name-based
    # (`owner-fqcn#member`, a constructor's member is the type's simple name); a consumer can't
    # reference a package/private method, so those are left off the cross-repo surface.
    if m.accessibility in ("public", "protected"):
        props["linkage_key"] = ids.extern_method_key(f"{owner_fqcn}#{m.name}")
    yield GraphNode("Method", {"id": mid}, props)
    yield GraphEdge("HAS_METHOD", type_label, {"id": type_id}, "Method", {"id": mid})
    yield GraphEdge("DEFINES", "File", {"id": file_id}, "Method", {"id": mid})  # per-file ownership (ADR 0005)
    for i, p in enumerate(m.parameters):
        yield from _emit_parameter(mid, "Method", p, i)


def _emit_parameter(parent_id: str, parent_label: str, param, position: int) -> Iterator[GraphNode | GraphEdge]:
    pid = ids.parameter_id(parent_id, position, param.name)
    yield GraphNode("Parameter", {"id": pid}, {
        "name": param.name, "type": param.type, "position": param.position,
        "is_varargs": param.is_varargs,
    })
    yield GraphEdge("HAS_PARAMETER", parent_label, {"id": parent_id}, "Parameter", {"id": pid})


def _emit_field(type_id: str, type_label: str, f) -> Iterator[GraphNode | GraphEdge]:
    fid = ids.field_id(type_id, f.name)
    yield GraphNode("Field", {"id": fid}, {
        "name": f.name, "type": f.type, "accessibility": f.accessibility,
        "is_static": "static" in f.modifiers, "is_final": "final" in f.modifiers,
        "annotations": [a.name for a in f.annotations],
    })
    yield GraphEdge("HAS_FIELD", type_label, {"id": type_id}, "Field", {"id": fid})


# -- pass 2: hierarchy, type references, calls ---------------------------------------------

def _resolve_target(name: str, type_index: dict[str, list[_Entry]], from_package: str, from_module: str) -> _Entry | None:
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
    if rt.superclass:  # class -> class
        target = _resolve_target(rt.superclass, type_index, e.package, e.module)
        if target is not None and target.type_id != e.type_id:
            yield GraphEdge("EXTENDS", e.kind, {"id": e.type_id}, target.kind, {"id": target.type_id})
    for iface in rt.interfaces:
        target = _resolve_target(iface, type_index, e.package, e.module)
        if target is None or target.type_id == e.type_id:
            continue
        # an interface's `interfaces` list is really its super-interfaces -> EXTENDS.
        rel = "EXTENDS" if e.kind == "Interface" else "IMPLEMENTS"
        yield GraphEdge(rel, e.kind, {"id": e.type_id}, target.kind, {"id": target.type_id})


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
        yield from _depends_on("Method", mid, names, e, type_index)
    for f in e.raw_type.fields:
        fid = ids.field_id(e.type_id, f.name)
        yield from _depends_on("Field", fid, set(_referenced_names(f.type)), e, type_index)


def _depends_on(from_label: str, from_id: str, names: set[str], e: _Entry, type_index) -> Iterator[GraphEdge]:
    emitted: set[str] = set()
    for name in names:
        target = _resolve_target(name, type_index, e.package, e.module)
        if target is None or target.type_id == e.type_id or target.type_id in emitted:
            continue
        emitted.add(target.type_id)
        yield GraphEdge("DEPENDS_ON", from_label, {"id": from_id}, target.kind, {"id": target.type_id})


def _pick(candidates: list[tuple[str, str, str, str]]) -> str:
    return sorted(candidates, key=lambda c: c[1])[0][1]


def _pick_method(candidates: list[tuple[str, str, str, str]], e: _Entry) -> str:
    """Method analog of _resolve_target's module/package bias: a `(owner_id, method_id, package,
    module)` candidate from the referrer's own module+package wins, then same-package, then
    same-module, then anywhere -- keeps two modules' same-named, same-package methods from cross-binding."""
    same_mod_pkg = [c for c in candidates if c[3] == e.module and c[2] == e.package]
    same_pkg = [c for c in candidates if c[2] == e.package]
    same_mod = [c for c in candidates if c[3] == e.module]
    return _pick(same_mod_pkg or same_pkg or same_mod or candidates)


def _resolve_call(call: RawCall, e: _Entry, member_types: dict[str, str], imports: dict[str, str],
                  type_index: dict[str, list[_Entry]], method_index: dict[str, list[tuple[str, str, str, str]]]):
    """Resolve one call to one of:
      * ``("local", target_method_id, extra_props)`` -- binds to a project Method (as before);
      * ``("extern", _Extern)`` -- a callee we can NAME but that isn't defined here (a JDK/stdlib
        or other-repo method) -> a placeholder, ADR 0004;
      * ``None`` -- a callee we genuinely can't name (a bare/inherited call), or an intra-project
        dangling reference (a missing member of a *project* type -- deliberately left dropped, so
        an incremental resync leaves no stale `extern://` behind).

    `new X()` -> X's constructor; a receiver naming a known field prefers that field's type's
    method; a bare / `this` call prefers the same type, then same package, then anywhere."""
    if call.is_new:
        target = _resolve_target(call.name, type_index, e.package, e.module)
        if target is not None:
            ctor = next((m for m in target.raw_type.methods if m.kind == "CONSTRUCTOR"), None)
            if ctor is not None:
                return "local", _method_id(target.type_id, ctor), {}
            if target.seed_ctor_id:  # a NON-emit target: its constructor id came from the graph seed
                return "local", target.seed_ctor_id, {}
            return None  # a project type's implicit/absent constructor -> intra-project, drop
        # `new` of a type not in this project -> external constructor (member = the type's name).
        return _extern_result(call.name, call.name, imports, receiver_type=None)

    candidates = method_index.get(call.name)
    if not candidates:
        # No project method of this name -> external if we can name the receiver's owning type.
        return _extern_call(call, e, member_types, imports, type_index)

    if call.receiver is not None and call.receiver != "this":
        rtype = member_types.get(call.receiver)
        if rtype is not None:
            resolved_project = False
            for type_name in _referenced_names(rtype):
                target_type = _resolve_target(type_name, type_index, e.package, e.module)
                if target_type is None:
                    continue
                resolved_project = True
                matches = [c for c in candidates if c[0] == target_type.type_id]
                if matches:
                    return "local", _pick(matches), {"receiver_type": target_type.name}
            if resolved_project:
                return None  # receiver is a project type, but missing this member -> intra-project
            # receiver's type is external -> name the call on that type (ADR 0004).
            head = next(iter(_referenced_names(rtype)), None)
            return _extern_result(head, call.name, imports, receiver_type=head) if head else None
        return "local", _pick_method(candidates, e), {}

    same_type = [c for c in candidates if c[0] == e.type_id]  # same-module already (type_id is module-scoped)
    if same_type:
        return "local", _pick(same_type), {}
    return "local", _pick_method(candidates, e), {}


def _extern_call(call: RawCall, e: _Entry, member_types: dict[str, str], imports: dict[str, str],
                 type_index: dict[str, list[_Entry]]):
    """A call with no project method of its name -> a placeholder when we can name the owning type
    (a field receiver's declared type, or a static `Type.method` receiver), else ``None`` (a bare
    `foo()` / `this.foo()` / a chained receiver: the owner is unknowable, so skip). A receiver that
    resolves to a *project* type is intra-project dangling -> dropped (GUARD, ADR 0004)."""
    recv = call.receiver
    if recv is None or recv == "this":
        return None  # unqualified / inherited call -- can't name the owning type
    rtype = member_types.get(recv)
    if rtype is not None:  # receiver is a field -> its declared type owns the method
        head = next(iter(_referenced_names(rtype)), None)
        if head is None:
            return None
        if _resolve_target(head, type_index, e.package, e.module) is not None:
            return None  # project type missing the member -> intra-project dangling, drop
        return _extern_result(head, call.name, imports, receiver_type=head)
    # A bare identifier receiver that isn't a field: a static `Type.method` call when it names a
    # type (import-bound, a known java.lang type, or capitalized per Java convention).
    if _resolve_target(recv, type_index, e.package, e.module) is not None:
        return None  # static call on a PROJECT type missing the member -> intra-project, drop
    if recv in imports or recv in _JAVA_LANG or recv[:1].isupper():
        return _extern_result(recv, call.name, imports, receiver_type=recv)
    return None  # a local variable of unknown type -- can't name the owning type


def _extern_result(type_simple: str, member: str, imports: dict[str, str], receiver_type: str | None):
    """Build the `("extern", _Extern)` result for a call on `type_simple.member` -- resolving the
    type's fqcn from this file's imports so the linkage key matches the real definition's."""
    fqcn = _fqcn(type_simple, imports)
    qualified = f"{fqcn}#{member}"
    return "extern", _Extern(
        linkage=ids.extern_method_key(qualified),
        name=member,
        qualified=qualified,
        origin=_origin(fqcn),
        receiver_type=receiver_type,
    )


def _emit_calls(e: _Entry, type_index: dict[str, list[_Entry]], method_index: dict[str, list[tuple[str, str, str, str]]],
                imports: dict[str, str], emitted_externs: set[str]) -> Iterator[GraphNode | GraphEdge]:
    member_types = {f.name: f.type for f in e.raw_type.fields if f.type}
    for m in e.raw_type.methods:
        mid = _method_id(e.type_id, m)
        emitted: set[str] = set()  # dedup CALLS targets from this method (project id or placeholder id)
        for call in m.calls:
            hit = _resolve_call(call, e, member_types, imports, type_index, method_index)
            if hit is None:
                continue
            if hit[0] == "local":
                _, target_id, extra = hit
                if target_id in emitted:
                    continue
                emitted.add(target_id)
                yield GraphEdge("CALLS", "Method", {"id": mid}, "Method", {"id": target_id}, {"resolution": "name", **extra})
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
                yield GraphEdge("CALLS", "Method", {"id": mid}, "Method", {"id": ext.linkage}, props)


# -- helpers -------------------------------------------------------------------------------

def _import_index(rf: RawFile) -> dict[str, str]:
    """Map a simple type name to its fqcn from this file's single-type imports, so a call on an
    external/JDK receiver can be named by its real fqcn (ADR 0004). Wildcard/static imports carry
    no simple-name -> fqcn mapping, so they're skipped; a plain (non-static) type import wins."""
    idx: dict[str, str] = {}
    for imp in rf.imports:
        if imp.is_wildcard or imp.is_static:
            continue
        idx.setdefault(imp.name.rsplit(".", 1)[-1], imp.name)
    return idx


def _fqcn(simple: str, imports: dict[str, str]) -> str:
    """Resolve a simple type name to its fully-qualified name via this file's imports, falling back
    to `java.lang` for the implicit types, else the bare simple name (still a stable linkage key)."""
    if simple in imports:
        return imports[simple]
    if simple in _JAVA_LANG:
        return f"java.lang.{simple}"
    return simple


def _type_fqcn(e: _Entry) -> str:
    """The type's fully-qualified name (matching its node's `qualified_name`), used as the owner
    portion of a real method's cross-repo linkage key."""
    return f"{e.package}.{e.name}" if e.package else e.name


def _external_method_node(ext: _Extern) -> GraphNode:
    """A placeholder for an unresolved call, keyed by its cross-repo linkage so a later ingest of
    the defining repo takes it over (ADR 0004). Shallow: shape mirrors a real Method so the same
    CALLS queries reach it; `resolved:false` + `origin` mark it."""
    return GraphNode("Method", {"id": ext.linkage}, {
        "name": ext.name, "qualified_name": ext.qualified, "resolved": False,
        "origin": ext.origin, "linkage_key": ext.linkage,
    })


def _norm(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def _basename(path: str) -> str:
    return _norm(path).rsplit("/", 1)[-1]
