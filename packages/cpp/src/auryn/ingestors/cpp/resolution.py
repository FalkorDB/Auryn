"""Project-wide pass (phase 2): turn the per-file RawFile list into graph nodes/edges, applying
the cross-file decisions a single file can't -- materializing the namespace tier, wiring each
out-of-line `Class::method` definition back to the class it belongs to (the key C++-specific
challenge), resolving EXTENDS/DEPENDS_ON to project types, and best-effort call resolution.

C++ is the structural union of the C and Java ingestors, so this pass is too: it carries Java's
namespace/type/member machinery (the tier here is `Namespace`, chained by HAS_NAMESPACE/
HAS_SUBNAMESPACE) *and* C's free-function/macro/#include layer. Two C++-specific twists:

  * out-of-line definitions -- `double geo::Shape::area() const {..}` is written at namespace or
    global scope, physically apart from `class Shape`. We split its `qualified_identifier`, match
    the owning type by simple name (preferring the enclosing namespace), and emit its Method
    under that class -- crucially with the *same* method id as the in-class declaration, so the
    declaration and the out-of-line definition merge into one node (as C merges a prototype with
    its definition). A qualified def whose owner isn't a project type degrades to a free function.
  * templates -- a class/method template's arity is folded into the type id (via ids.py), so
    `Box` and `Box<T>` can't collide (mirroring Java's generics handling).

C++ has no interfaces, so there is **no IMPLEMENTS** edge: an abstract/pure-virtual "interface"
class is just a Class, and deriving from it is still EXTENDS.

Name-based only -- this is the fast tree-sitter engine, so every CALLS/DEPENDS_ON edge is stamped
`resolution="name"`. A future libclang deep pass (template instantiation, overload resolution,
ADL, precise header linking) would re-emit the same node ids with a stronger provenance (the
two-phase model). Ambiguous or unresolved references produce no edge rather than a wrong guess --
the same trade-off as the C/Java/C# ingestors."""

from __future__ import annotations

import posixpath
import re
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field

from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.ingestors.cpp import ids
from auryn.ingestors.cpp.raw_model import RawCall, RawFile, RawFunction, RawMethod, RawType

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Type tokens that are never a project type -- C++ primitives/keywords appearing in type text.
_NON_TYPE_TOKENS = frozenset(
    {"void", "int", "long", "short", "char", "bool", "float", "double", "signed", "unsigned",
     "const", "volatile", "auto", "wchar_t", "char8_t", "char16_t", "char32_t", "size_t",
     "struct", "class", "enum", "union", "typename", "static", "mutable", "constexpr"}
)


@dataclass
class _Entry:
    type_id: str
    kind: str  # "Class" | "Struct" | "Union" | "Enum"
    name: str
    namespace: str
    raw_type: RawType
    file_id: str
    parent_type_id: str | None
    scope_id: str
    scope_label: str | None  # "Namespace" | "Project" (top-level only; None if nested)


@dataclass
class _MethodRec:
    """One method after merging an (optional) in-class declaration with an (optional) out-of-line
    definition into a single node (definition wins for body/source/calls)."""

    method_id: str
    name: str
    kind: str
    return_type: str | None
    params: list  # RawParameter
    is_static: bool
    is_virtual: bool
    is_const: bool
    defined: bool
    calls: list  # RawCall
    source: str
    doc: str
    line_start: int
    line_end: int


@dataclass
class _FuncRec:
    function_id: str
    scope_id: str
    scope_label: str
    namespace: str
    raw: RawFunction


@dataclass
class _MethodIndexEntry:
    owner_type_id: str
    method_id: str
    kind: str
    namespace: str


@dataclass
class _TypeStub:
    """A non-emit type seeded from the graph for incremental resolution -- duck-compatible with
    `_Entry` everywhere `_resolve_type` is used (only .type_id/.kind/.name/.namespace are read)."""
    type_id: str
    kind: str
    name: str
    namespace: str


@dataclass
class _FuncStub:
    """A non-emit free function seeded from the graph -- duck-compatible with `_FuncRec` everywhere
    `_pick_function`/call resolution reads it (only .function_id/.namespace)."""
    function_id: str
    namespace: str


@dataclass
class CppGraphIndex:
    """Resolution tables seeded from the GRAPH for the NON-emit files (ADR 0005), so
    `resolve_incremental` resolves an emit file's cross-file EXTENDS/DEPENDS_ON/CALLS/#include against
    the surviving project without re-parsing it. Reconstructed by `cpp.incremental.read_cpp_index`."""
    type_index: dict = field(default_factory=dict)        # simple name -> [_TypeStub]
    method_index: dict = field(default_factory=dict)      # method name -> [_MethodIndexEntry] (incl ctors)
    function_index: dict = field(default_factory=dict)    # function name -> [_FuncStub]
    include_fullpaths: dict = field(default_factory=dict)  # normalized full path -> File id (NO basename keys;
    # basename collapse happens in resolve_incremental over the seed+emit UNION, so a basename shared by a
    # surviving and an emit file is correctly seen as ambiguous -- matching the full path, see _collapse_basenames)
    # ids DEFINED by a surviving (non-emit) file: a forward decl / prototype in the emit set for one of
    # these emits only its DECLARES provenance, never a sparse node that would clobber the real definition.
    defined_type_ids: frozenset = frozenset()
    defined_function_ids: frozenset = frozenset()


def resolve(raw_files: list[RawFile], project_id: str) -> Iterator[GraphNode | GraphEdge]:
    # Stable path order so a symbol DEFINED in several files (multi-program dirs: three `class Benchmark`
    # benchmark mains, several `main`) reduces to an order-INDEPENDENT winner -- identical here and in
    # resolve_incremental's emit subset, the precondition for incremental == full (ADR 0005, as C/Java do).
    raw_files = sorted(raw_files, key=lambda rf: _norm(rf.path))
    file_ids: dict[str, str] = {}
    namespaces_seen: set[str] = set()
    # Per-entity file provenance (ADR 0005): entity_id -> {file_id -> is_definition}. A file that
    # DEFINES an entity (`True`, class body / method or function body) emits File-DEFINES; one that only
    # DECLARES it (`False`, forward decl / in-class method decl whose body is out-of-line / prototype)
    # emits File-DECLARES. Namespaces have no decl/def split -> a plain set of contributing files.
    type_prov: dict[str, tuple[str, dict[str, bool]]] = {}
    method_prov: dict[str, tuple[str, dict[str, bool]]] = {}
    func_prov: dict[str, tuple[str, dict[str, bool]]] = {}
    ns_prov: dict[str, set[str]] = defaultdict(set)

    def note_ns(ns: str, fid: str) -> None:
        namespaces_seen.add(ns)
        prefix = ""
        for segment in ns.split("::"):
            prefix = f"{prefix}::{segment}" if prefix else segment
            ns_prov[ids.namespace_id(project_id, prefix)].add(fid)

    for rf in raw_files:
        fid = ids.file_id(project_id, rf.path)
        file_ids[rf.path] = fid
        yield GraphNode("File", {"id": fid}, {"name": _basename(rf.path), "path": _norm(rf.path)})
        yield GraphEdge("HAS_FILE", "Project", {"id": project_id}, "File", {"id": fid})
        for t in rf.types:
            if t.namespace:
                note_ns(t.namespace, fid)
        for f in rf.functions:
            if f.namespace:
                note_ns(f.namespace, fid)
    yield from _emit_namespaces(namespaces_seen, project_id)

    # Flatten every (possibly nested) type into a scoped entry.
    entries: list[_Entry] = []
    for rf in raw_files:
        for rt in rf.types:
            _flatten(rt, file_ids[rf.path], project_id, None, entries)

    type_index: dict[str, list[_Entry]] = defaultdict(list)
    for e in entries:
        type_index[e.name].append(e)
        _note(type_prov, e.type_id, e.kind, e.file_id, e.raw_type.is_definition)
        # In-class method declarations/inline-definitions are provenanced to the type's file.
        for m in e.raw_type.methods:
            mid = ids.method_id(e.type_id, m.name, [p.type for p in m.params])
            _note(method_prov, mid, "Method", e.file_id, m.is_definition)

    # Wire out-of-line member definitions back to their owning class (or degrade to free fns).
    defs_by_type: dict[str, list[tuple[str, RawMethod]]] = defaultdict(list)
    extra_functions: list[tuple[str, RawFunction]] = []  # (rel_path, converted free function)
    for rf in raw_files:
        for md in rf.member_defs:
            inferred_ns = _join_ns([md.namespace, *md.owner_scope[:-1]])
            owner = _resolve_type(md.owner_name or "", inferred_ns, type_index)
            if owner is not None:
                defs_by_type[owner.type_id].append((file_ids[rf.path], md))
                mid = ids.method_id(owner.type_id, md.name, [p.type for p in md.params])
                _note(method_prov, mid, "Method", file_ids[rf.path], md.is_definition)
            else:  # e.g. `int myns::helper()` -- a namespace-qualified free function definition
                extra_functions.append((rf.path, _method_to_function(md, inferred_ns)))

    # Merge in-class declarations with out-of-line definitions -> one _MethodRec per method.
    methods_by_type: dict[str, list[_MethodRec]] = {}
    method_index: dict[str, list[_MethodIndexEntry]] = defaultdict(list)
    for e in entries:
        recs = _merge_methods(e, [md for _f, md in defs_by_type.get(e.type_id, [])])
        methods_by_type[e.type_id] = recs
        for r in recs:
            method_index[r.name].append(_MethodIndexEntry(e.type_id, r.method_id, r.kind, e.namespace))

    # Free functions (from files + degraded qualified defs), scoped to a namespace/project.
    func_recs: list[_FuncRec] = []
    function_index: dict[str, list[_FuncRec]] = defaultdict(list)
    for rf in raw_files:
        for fn in rf.functions:
            fr = _make_func_rec(fn, project_id)
            func_recs.append(fr)
            _note(func_prov, fr.function_id, "Function", file_ids[rf.path], fn.is_definition)
    for path, fn in extra_functions:
        fr = _make_func_rec(fn, project_id)
        func_recs.append(fr)
        _note(func_prov, fr.function_id, "Function", file_ids[path], fn.is_definition)
    # Deduplicate free functions sharing an id (a prototype + its definition), definition wins.
    fr_by_id: dict[str, _FuncRec] = {}
    for fr in func_recs:
        prev = fr_by_id.get(fr.function_id)
        if prev is None or (fr.raw.is_definition and not prev.raw.is_definition):
            fr_by_id[fr.function_id] = fr
    func_recs = list(fr_by_id.values())
    for fr in func_recs:
        function_index[fr.raw.name].append(fr)

    # Pass 1: nodes + structural edges (so pass 2's cross-refs always have a landing node).
    for e in entries:
        yield from _emit_type_and_members(e, methods_by_type[e.type_id])
    for fr in func_recs:
        yield from _emit_function(fr)

    # File provenance (ADR 0005): File-DEFINES/DECLARES -> every program-global entity + namespace.
    yield from _emit_provenance(type_prov, method_prov, func_prov, ns_prov)

    # Pass 2: hierarchy, type references, calls. `externs` dedups placeholder Function nodes for
    # unresolved (std/system/other-repo) calls across the whole project (ADR 0004).
    externs: set[str] = set()
    for e in entries:
        yield from _emit_hierarchy(e, type_index)
        yield from _emit_type_references(e, methods_by_type[e.type_id], type_index)
        yield from _emit_calls_for_type(e, methods_by_type[e.type_id], type_index, method_index, function_index, externs)
    for fr in func_recs:
        yield from _emit_function_depends_on(fr, type_index)
        yield from _emit_function_calls(fr, type_index, method_index, function_index, externs)

    # C-family: macros (file/preprocessor-scoped, mirrors the C ingestor) + resolved #includes.
    yield from _emit_macros(raw_files, project_id)
    yield from _emit_includes(raw_files, file_ids, _build_include_index(raw_files, file_ids))


def callable_locations(raw_files: list[RawFile], project_id: str):
    """Every C++ function/method's SCIP-join location (graph id, DEFINITION file, 1-based def line span,
    name, called names) -- the bridge the deep (scip-clang / SCIP) pass needs. Reuses the fast pass's own
    _flatten / _merge_methods / _make_func_rec / _resolve_type so ids are byte-identical, and keys each
    location to the DEFINITION file: scip-clang reports a callable's occurrences (its def AND the calls in
    its body) in the file where the body lives, which for an out-of-line member (`Class::m` in a .cc,
    declared in a .h) is NOT the declaring file. A parity test asserts these ids subset resolve()'s
    Function/Method node ids. The core.scip_resolve import is deferred here (deep-only) so the fast pass
    never requires it."""
    from auryn.core.scip_resolve import CallableLocation

    raw_files = sorted(raw_files, key=lambda rf: _norm(rf.path))
    file_ids = {rf.path: ids.file_id(project_id, rf.path) for rf in raw_files}
    path_by_fid = {fid: _norm(p) for p, fid in file_ids.items()}

    entries: list[_Entry] = []
    for rf in raw_files:
        for rt in rf.types:
            _flatten(rt, file_ids[rf.path], project_id, None, entries)
    type_index: dict[str, list[_Entry]] = defaultdict(list)
    for e in entries:
        type_index[e.name].append(e)

    # def file per callable id -- the file whose occurrence is_definition (a body), preferred over a decl.
    def_file: dict[str, str] = {}
    for e in entries:  # in-class declarations / inline definitions
        for m in e.raw_type.methods:
            mid = ids.method_id(e.type_id, m.name, [p.type for p in m.params])
            if m.is_definition or mid not in def_file:
                def_file[mid] = e.file_id

    defs_by_type: dict[str, list[RawMethod]] = defaultdict(list)
    extra_functions: list[tuple[str, RawFunction]] = []
    for rf in raw_files:  # out-of-line member definitions
        for md in rf.member_defs:
            inferred_ns = _join_ns([md.namespace, *md.owner_scope[:-1]])
            owner = _resolve_type(md.owner_name or "", inferred_ns, type_index)
            if owner is not None:
                defs_by_type[owner.type_id].append(md)
                mid = ids.method_id(owner.type_id, md.name, [p.type for p in md.params])
                if md.is_definition:  # the out-of-line body wins
                    def_file[mid] = file_ids[rf.path]
            else:
                extra_functions.append((rf.path, _method_to_function(md, inferred_ns)))

    out = []
    for e in entries:  # methods: merged decl+def (def-preferred line span), keyed to the def file
        for r in _merge_methods(e, defs_by_type.get(e.type_id, [])):
            fid = def_file.get(r.method_id, e.file_id)
            out.append(CallableLocation(r.method_id, "Method", path_by_fid.get(fid, ""),
                                        r.line_start, r.line_end, r.name,
                                        frozenset(c.name for c in r.calls)))

    # free functions (incl. degraded namespace-qualified defs): dedup by id, definition wins. Keep the
    # chosen record's file WITH it (the same (file, line span, calls) must stay together) -- separate
    # first-def / last-def bookkeeping would pair one body's span with another TU's file.
    chosen: dict[str, tuple] = {}  # function_id -> (_FuncRec, def_file_id)
    for path, fn in [(rf.path, fn) for rf in raw_files for fn in rf.functions] + extra_functions:
        fr = _make_func_rec(fn, project_id)
        prev = chosen.get(fr.function_id)
        if prev is None or (fr.raw.is_definition and not prev[0].raw.is_definition):
            chosen[fr.function_id] = (fr, file_ids[path])
    for fr, fid in chosen.values():
        out.append(CallableLocation(fr.function_id, "Function", path_by_fid.get(fid, ""),
                                    fr.raw.line_start, fr.raw.line_end, fr.raw.name,
                                    frozenset(c.name for c in fr.raw.calls)))
    return out


def resolve_incremental(emit_raws: list[RawFile], project_id: str, index: CppGraphIndex) -> Iterator[GraphNode | GraphEdge]:
    """Emit graph items for ONLY `emit_raws`, resolving cross-file references against `index` (the
    graph-seeded non-emit symbols) merged with the emit files' own. The blast-radius analogue of
    `resolve()` (ADR 0005) -- SAME emitters, scope seeded rather than whole-project. The engine's
    closures guarantee every file co-authoring a shared node an emit file touches is in `emit_raws`:
    for C++'s out-of-line methods that means BOTH the class-body file (.h) AND the definition file (.cc)
    are always present together (forward co-member + reverse member-owner closures), so a method is never
    sparsely re-emitted. A forward-declared type / free-function prototype whose DEFINITION is in a
    surviving file emits its DECLARES provenance only (never a sparse node that would clobber the real
    definition) -- mirrors C."""
    emit_raws = sorted(emit_raws, key=lambda rf: _norm(rf.path))  # same stable order as resolve() (winner determinism)
    file_ids = {rf.path: ids.file_id(project_id, rf.path) for rf in emit_raws}
    type_prov: dict[str, tuple[str, dict[str, bool]]] = {}
    method_prov: dict[str, tuple[str, dict[str, bool]]] = {}
    func_prov: dict[str, tuple[str, dict[str, bool]]] = {}
    ns_prov: dict[str, set[str]] = defaultdict(set)
    namespaces_seen: set[str] = set()

    def note_ns(ns: str, fid: str) -> None:
        namespaces_seen.add(ns)
        prefix = ""
        for segment in ns.split("::"):
            prefix = f"{prefix}::{segment}" if prefix else segment
            ns_prov[ids.namespace_id(project_id, prefix)].add(fid)

    for rf in emit_raws:
        fid = file_ids[rf.path]
        yield GraphNode("File", {"id": fid}, {"name": _basename(rf.path), "path": _norm(rf.path)})
        yield GraphEdge("HAS_FILE", "Project", {"id": project_id}, "File", {"id": fid})
        for t in rf.types:
            if t.namespace:
                note_ns(t.namespace, fid)
        for f in rf.functions:
            if f.namespace:
                note_ns(f.namespace, fid)
    yield from _emit_namespaces(namespaces_seen, project_id)

    # Flatten the emit set's types, recording provenance.
    entries: list[_Entry] = []
    for rf in emit_raws:
        for rt in rf.types:
            _flatten(rt, file_ids[rf.path], project_id, None, entries)
    emit_type_ids = {e.type_id for e in entries}

    # Merged type index: seed stubs (non-emit) + the emit set's own entries. Both expose
    # .type_id/.kind/.name/.namespace, which is all `_resolve_type` reads.
    type_index: dict[str, list] = defaultdict(list)
    for name, stubs in index.type_index.items():
        type_index[name].extend(stubs)
    for e in entries:
        type_index[e.name].append(e)
        _note(type_prov, e.type_id, e.kind, e.file_id, e.raw_type.is_definition)
        for m in e.raw_type.methods:
            mid = ids.method_id(e.type_id, m.name, [p.type for p in m.params])
            _note(method_prov, mid, "Method", e.file_id, m.is_definition)

    # Wire out-of-line member defs to their owning type via the MERGED index (so an emit .cc's method
    # def binds to a type whether the class body is in the emit set or seeded).
    defs_by_type: dict[str, list[tuple[str, RawMethod]]] = defaultdict(list)
    extra_functions: list[tuple[str, RawFunction]] = []
    for rf in emit_raws:
        for md in rf.member_defs:
            inferred_ns = _join_ns([md.namespace, *md.owner_scope[:-1]])
            owner = _resolve_type(md.owner_name or "", inferred_ns, type_index)
            if owner is not None:
                defs_by_type[owner.type_id].append((file_ids[rf.path], md))
                mid = ids.method_id(owner.type_id, md.name, [p.type for p in md.params])
                _note(method_prov, mid, "Method", file_ids[rf.path], md.is_definition)
            else:
                extra_functions.append((rf.path, _method_to_function(md, inferred_ns)))

    # Merge in-class decls with out-of-line defs per emit type; build the merged method index.
    methods_by_type: dict[str, list[_MethodRec]] = {}
    method_index: dict[str, list[_MethodIndexEntry]] = defaultdict(list)
    for name, es in index.method_index.items():
        method_index[name].extend(es)
    for e in entries:
        recs = _merge_methods(e, [md for _f, md in defs_by_type.get(e.type_id, [])])
        methods_by_type[e.type_id] = recs
        for r in recs:
            method_index[r.name].append(_MethodIndexEntry(e.type_id, r.method_id, r.kind, e.namespace))

    # Free functions (emit files + degraded qualified defs); merged function index.
    func_recs: list[_FuncRec] = []
    function_index: dict[str, list] = defaultdict(list)
    for name, stubs in index.function_index.items():
        function_index[name].extend(stubs)
    for rf in emit_raws:
        for fn in rf.functions:
            fr = _make_func_rec(fn, project_id)
            func_recs.append(fr)
            _note(func_prov, fr.function_id, "Function", file_ids[rf.path], fn.is_definition)
    for path, fn in extra_functions:
        fr = _make_func_rec(fn, project_id)
        func_recs.append(fr)
        _note(func_prov, fr.function_id, "Function", file_ids[path], fn.is_definition)
    fr_by_id: dict[str, _FuncRec] = {}
    for fr in func_recs:
        prev = fr_by_id.get(fr.function_id)
        if prev is None or (fr.raw.is_definition and not prev.raw.is_definition):
            fr_by_id[fr.function_id] = fr
    func_recs = list(fr_by_id.values())
    for fr in func_recs:
        function_index[fr.raw.name].append(fr)

    # Pass 1: nodes + structural edges. A forward-decl of a type DEFINED in a surviving file emits no
    # node (DECLARES provenance only, below) -- a sparse re-emit would clobber the real definition.
    for e in entries:
        if not e.raw_type.is_definition and e.type_id in index.defined_type_ids:
            # Forward decl of a type DEFINED in a surviving file: emit no (sparse) node that would clobber
            # the real definition, but still emit this file's DEFINED_IN edge -- a full resolve emits one
            # per type occurrence (incl. forward decls), so omitting it would diverge (PR #18 review).
            yield GraphEdge("DEFINED_IN", e.kind, {"id": e.type_id}, "File", {"id": e.file_id})
            continue
        yield from _emit_type_and_members(e, methods_by_type[e.type_id])
    for fr in func_recs:
        if not fr.raw.is_definition and fr.function_id in index.defined_function_ids:
            continue  # prototype of a function defined elsewhere -> DECLARES provenance only
        yield from _emit_function(fr)

    # Pass 2: hierarchy, type refs, calls -- resolved against the merged indexes.
    externs: set[str] = set()
    for e in entries:
        yield from _emit_hierarchy(e, type_index)
        yield from _emit_type_references(e, methods_by_type[e.type_id], type_index)
        yield from _emit_calls_for_type(e, methods_by_type[e.type_id], type_index, method_index, function_index, externs)
    for fr in func_recs:
        yield from _emit_function_depends_on(fr, type_index)
        yield from _emit_function_calls(fr, type_index, method_index, function_index, externs)

    yield from _emit_macros(emit_raws, project_id)
    # Includes resolve against the WHOLE project. Union the seed's full-path map with the emit files', THEN
    # collapse basenames over that union -- so a basename shared by a surviving file and an emit file is seen
    # as ambiguous (no bare-basename key), exactly as the full path sees it. Collapsing each half separately
    # would resolve an #include the full recompute leaves dangling (incremental != full).
    union_fullpaths = {**index.include_fullpaths, **_include_fullpaths(emit_raws, file_ids)}
    yield from _emit_includes(emit_raws, file_ids, _collapse_basenames(union_fullpaths))
    yield from _emit_provenance(type_prov, method_prov, func_prov, ns_prov)


# -- namespaces ----------------------------------------------------------------------------

def _emit_namespaces(namespaces: set[str], project_id: str) -> Iterator[GraphNode | GraphEdge]:
    """Materialize every prefix of every declared namespace (as Java does for packages / C# for
    namespaces): the first segment hangs off the Project via HAS_NAMESPACE, deeper segments chain
    via HAS_SUBNAMESPACE. `a::b::c` -> Namespace(a) -> Namespace(a::b) -> Namespace(a::b::c)."""
    emitted: set[str] = set()
    for ns in namespaces:
        prefix = ""
        parent_id, parent_label, parent_rel = project_id, "Project", "HAS_NAMESPACE"
        for segment in ns.split("::"):
            prefix = f"{prefix}::{segment}" if prefix else segment
            nid = ids.namespace_id(project_id, prefix)
            if nid not in emitted:
                emitted.add(nid)
                yield GraphNode("Namespace", {"id": nid}, {"name": segment, "qualified_name": prefix})
                yield GraphEdge(parent_rel, parent_label, {"id": parent_id}, "Namespace", {"id": nid})
            parent_id, parent_label, parent_rel = nid, "Namespace", "HAS_SUBNAMESPACE"


# -- flattening ----------------------------------------------------------------------------

def _flatten(rt: RawType, file_id: str, project_id: str, parent_type_id: str | None, entries: list[_Entry]) -> None:
    if parent_type_id is not None:
        tid = ids.type_id(parent_type_id, rt.kind, rt.name, rt.type_parameters)
        scope_id, scope_label = parent_type_id, None
    elif rt.namespace:
        scope_id = ids.namespace_id(project_id, rt.namespace)
        scope_label = "Namespace"
        tid = ids.type_id(scope_id, rt.kind, rt.name, rt.type_parameters)
    else:
        scope_id, scope_label = project_id, "Project"
        tid = ids.type_id(scope_id, rt.kind, rt.name, rt.type_parameters)

    entries.append(_Entry(tid, rt.kind, rt.name, rt.namespace, rt, file_id, parent_type_id, scope_id, scope_label))
    for nested in rt.nested_types:
        _flatten(nested, file_id, project_id, tid, entries)


# -- method merging ------------------------------------------------------------------------

def _merge_methods(e: _Entry, out_of_line: list[RawMethod]) -> list[_MethodRec]:
    """One _MethodRec per method id: an in-class declaration and its out-of-line definition
    (matched by owner + name + parameter *types*) collapse into a single node, the definition
    supplying the body/source/calls (as C merges a function prototype with its definition)."""
    recs: dict[str, _MethodRec] = {}
    order: list[str] = []

    def upsert(m: RawMethod) -> None:
        mid = ids.method_id(e.type_id, m.name, [p.type for p in m.params])
        cur = recs.get(mid)
        if cur is None:
            recs[mid] = _MethodRec(
                method_id=mid, name=m.name, kind=m.kind, return_type=m.return_type,
                params=list(m.params), is_static=m.is_static, is_virtual=m.is_virtual,
                is_const=m.is_const, defined=m.is_definition, calls=list(m.calls),
                source=m.source, doc=m.doc, line_start=m.line_start, line_end=m.line_end,
            )
            order.append(mid)
            return
        # Merge: the definition wins for body-derived fields; keep declaration flags where richer.
        if m.is_definition and not cur.defined:
            cur.defined = True
            cur.calls = list(m.calls)
            cur.source = m.source
            cur.line_start, cur.line_end = m.line_start, m.line_end
            cur.params = list(m.params)  # the definition's params carry names
        cur.is_virtual = cur.is_virtual or m.is_virtual
        cur.is_static = cur.is_static or m.is_static
        if cur.return_type is None and m.return_type is not None:
            cur.return_type = m.return_type
        if m.doc and not cur.doc:  # keep the first non-empty doc (decl usually carries the API doc)
            cur.doc = m.doc

    for m in e.raw_type.methods:
        upsert(m)
    for m in out_of_line:
        upsert(m)
    return [recs[mid] for mid in order]


# -- pass 1: nodes + structural edges ------------------------------------------------------

def _type_properties(rt: RawType) -> dict:
    props: dict = {
        "name": rt.name,
        "qualified_name": f"{rt.namespace}::{rt.name}" if rt.namespace else rt.name,
        "type_parameters": list(rt.type_parameters),
        "is_template": bool(rt.type_parameters),
        "source": rt.source,
        "doc": rt.doc,
        "line_start": rt.line_start,
        "line_end": rt.line_end,
    }
    if rt.kind == "Enum":
        props["is_scoped"] = rt.is_scoped  # `enum class` / `enum struct`
        props["members"] = list(rt.enum_members)
    return props


def _emit_type_and_members(e: _Entry, methods: list[_MethodRec]) -> Iterator[GraphNode | GraphEdge]:
    yield GraphNode(e.kind, {"id": e.type_id}, _type_properties(e.raw_type))
    if e.parent_type_id is not None:
        yield GraphEdge("HAS_NESTED_TYPE", _parent_kind(e), {"id": e.parent_type_id}, e.kind, {"id": e.type_id})
    else:
        yield GraphEdge("HAS_TYPE", e.scope_label, {"id": e.scope_id}, e.kind, {"id": e.type_id})
    yield GraphEdge("DEFINED_IN", e.kind, {"id": e.type_id}, "File", {"id": e.file_id})

    for r in methods:
        yield from _emit_method(e.type_id, e.kind, r)
    for f in e.raw_type.fields:
        fid = ids.field_id(e.type_id, f.name)
        yield GraphNode("Field", {"id": fid}, {
            "name": f.name, "type": f.type, "is_static": f.is_static,
            "line_start": f.line_start, "line_end": f.line_end,
        })
        yield GraphEdge("HAS_FIELD", e.kind, {"id": e.type_id}, "Field", {"id": fid})
    for member in e.raw_type.enum_members:
        mid = ids.enum_member_id(e.type_id, member)
        yield GraphNode("EnumMember", {"id": mid}, {"name": member})
        yield GraphEdge("HAS_ENUM_MEMBER", "Enum", {"id": e.type_id}, "EnumMember", {"id": mid})


_KIND_BY_SLUG = {"class": "Class", "struct": "Struct", "union": "Union", "enum": "Enum"}


def _parent_kind(e: _Entry) -> str:
    # parent_type_id ends with ".<slug>.<name>[`arity]"; pull the slug.
    tail = e.parent_type_id.rsplit(".", 2)[-2] if e.parent_type_id else "class"
    return _KIND_BY_SLUG.get(tail, "Class")


def _emit_method(type_id: str, type_label: str, r: _MethodRec) -> Iterator[GraphNode | GraphEdge]:
    yield GraphNode("Method", {"id": r.method_id}, {
        "name": r.name,
        "kind": r.kind,
        "is_static": r.is_static,
        "is_virtual": r.is_virtual,
        "is_const": r.is_const,
        "defined": r.defined,
        "return_type": r.return_type,
        "source": r.source,
        "doc": r.doc,
        "line_start": r.line_start,
        "line_end": r.line_end,
    })
    yield GraphEdge("HAS_METHOD", type_label, {"id": type_id}, "Method", {"id": r.method_id})
    for i, p in enumerate(r.params):
        yield from _emit_parameter(r.method_id, "Method", p, i)


def _emit_parameter(parent_id: str, parent_label: str, param, position: int) -> Iterator[GraphNode | GraphEdge]:
    pname = param.name or f"arg{position}"
    pid = ids.parameter_id(parent_id, position, pname)
    yield GraphNode("Parameter", {"id": pid}, {"name": pname, "type": param.type, "position": position})
    yield GraphEdge("HAS_PARAMETER", parent_label, {"id": parent_id}, "Parameter", {"id": pid})


def _make_func_rec(fn: RawFunction, project_id: str) -> _FuncRec:
    if fn.namespace:
        scope_id, scope_label = ids.namespace_id(project_id, fn.namespace), "Namespace"
    else:
        scope_id, scope_label = project_id, "Project"
    fid = ids.function_id(scope_id, fn.name, [p.type for p in fn.params])
    return _FuncRec(function_id=fid, scope_id=scope_id, scope_label=scope_label, namespace=fn.namespace, raw=fn)


def _cpp_qualified(namespace: str | None, name: str) -> str:
    return f"{namespace}::{name}" if namespace else name


def _cpp_origin(qualified: str) -> str:
    head = qualified.split("::", 1)[0]
    if head in ("std", "__gnu_cxx"):
        return "stdlib"
    return "external"


def _external_free_call(call: RawCall) -> str | None:
    """The qualified free-function name for an unresolved call we can name across repos (ADR 0004),
    or None for a constructor / receiver-typed member call we can't attribute to a free function."""
    if call.is_new or (call.receiver is not None and call.receiver != "this"):
        return None
    return f"{call.scope}::{call.name}" if call.scope else call.name


def _external_function_node(qualified: str) -> GraphNode:
    eid = ids.extern_function_key(qualified)
    return GraphNode("Function", {"id": eid}, {
        "name": qualified.rsplit("::", 1)[-1], "qualified_name": qualified,
        "resolved": False, "origin": _cpp_origin(qualified), "linkage_key": eid,
    })


def _emit_function(fr: _FuncRec) -> Iterator[GraphNode | GraphEdge]:
    fn = fr.raw
    qualified = _cpp_qualified(fn.namespace, fn.name)
    yield GraphNode("Function", {"id": fr.function_id}, {
        "name": fn.name,
        "qualified_name": qualified,
        "linkage_key": ids.extern_function_key(qualified),  # cross-repo key (ADR 0004)
        "return_type": fn.return_type,
        "signature": fn.signature,
        "defined": fn.is_definition,
        "source": fn.source,
        "doc": fn.doc,
        "line_start": fn.line_start,
        "line_end": fn.line_end,
    })
    yield GraphEdge("HAS_FUNCTION", fr.scope_label, {"id": fr.scope_id}, "Function", {"id": fr.function_id})
    for i, p in enumerate(fn.params):
        yield from _emit_parameter(fr.function_id, "Function", p, i)


# -- pass 2: hierarchy, type references, calls ---------------------------------------------

def _resolve_type(name: str, from_namespace: str, type_index: dict[str, list[_Entry]]) -> _Entry | None:
    matches = type_index.get(name)
    if not matches:
        return None
    same = [m for m in matches if m.namespace == from_namespace]
    pool = same or matches
    return sorted(pool, key=lambda m: m.type_id)[0]  # deterministic on ambiguity


def _emit_hierarchy(e: _Entry, type_index: dict[str, list[_Entry]]) -> Iterator[GraphEdge]:
    # C++ has no interfaces -> EXTENDS only, one edge per base (multiple inheritance), carrying
    # the access specifier. A pure-virtual "interface" base is just another Class -> still EXTENDS.
    emitted: set[str] = set()
    for base in e.raw_type.bases:
        target = _resolve_type(base.name, e.namespace, type_index)
        if target is None or target.type_id == e.type_id or target.type_id in emitted:
            continue
        emitted.add(target.type_id)
        yield GraphEdge("EXTENDS", e.kind, {"id": e.type_id}, target.kind, {"id": target.type_id},
                        {"access": base.access})


def _referenced_names(type_text: str | None) -> list[str]:
    if not type_text:
        return []
    return [n for n in _IDENTIFIER_RE.findall(type_text) if n not in _NON_TYPE_TOKENS]


def _depends_on(from_label: str, from_id: str, names: set[str], from_namespace: str,
                self_type_id: str | None, type_index) -> Iterator[GraphEdge]:
    emitted: set[str] = set()
    for name in names:
        target = _resolve_type(name, from_namespace, type_index)
        if target is None or target.type_id == self_type_id or target.type_id in emitted:
            continue
        emitted.add(target.type_id)
        yield GraphEdge("DEPENDS_ON", from_label, {"id": from_id}, target.kind, {"id": target.type_id})


def _emit_type_references(e: _Entry, methods: list[_MethodRec], type_index) -> Iterator[GraphEdge]:
    for r in methods:
        names = set(_referenced_names(r.return_type))
        for p in r.params:
            names.update(_referenced_names(p.type))
        yield from _depends_on("Method", r.method_id, names, e.namespace, e.type_id, type_index)
    for f in e.raw_type.fields:
        fid = ids.field_id(e.type_id, f.name)
        yield from _depends_on("Field", fid, set(_referenced_names(f.type)), e.namespace, e.type_id, type_index)


def _emit_function_depends_on(fr: _FuncRec, type_index) -> Iterator[GraphEdge]:
    fn = fr.raw
    names = set(_referenced_names(fn.return_type))
    for p in fn.params:
        names.update(_referenced_names(p.type))
    yield from _depends_on("Function", fr.function_id, names, fr.namespace, None, type_index)


def _pick_method(candidates: list[_MethodIndexEntry]) -> str:
    return sorted(candidates, key=lambda c: c.method_id)[0].method_id


def _pick_function(candidates: list[_FuncRec], from_namespace: str) -> str:
    same = [c for c in candidates if c.namespace == from_namespace]
    pool = same or candidates
    return sorted(pool, key=lambda c: c.function_id)[0].function_id


def _resolve_call(call: RawCall, from_namespace: str, current_type_id: str | None, member_types: dict[str, str],
                  type_index, method_index: dict[str, list[_MethodIndexEntry]],
                  function_index: dict[str, list[_FuncRec]]):
    """(target_label, target_id, extra_props) or None. `new X()` -> X's constructor; a
    `Class::x()` qualified call -> that class's method (else a namespace free function); a
    receiver naming a known field/param -> that type's method; a bare/`this` call -> the same
    type's method (implicit this), else a free function."""
    if call.is_new:
        target = _resolve_type(call.name, from_namespace, type_index)
        if target is not None:
            ctors = [c for c in method_index.get(call.name, [])
                     if c.owner_type_id == target.type_id and c.kind == "CONSTRUCTOR"]
            if ctors:
                return "Method", _pick_method(ctors), {}
        return None

    if call.scope:
        target = _resolve_type(call.scope, from_namespace, type_index)
        if target is not None:
            matches = [c for c in method_index.get(call.name, []) if c.owner_type_id == target.type_id]
            if matches:
                return "Method", _pick_method(matches), {"receiver_type": target.name}
        fns = function_index.get(call.name)  # `ns::free_function()`
        if fns:
            return "Function", _pick_function(fns, from_namespace), {}
        return None

    if call.receiver is not None and call.receiver != "this":
        rtype = member_types.get(call.receiver)
        if rtype is None:
            return None  # unknown receiver (a local variable we don't track) -> no guess
        for type_name in _referenced_names(rtype):
            target = _resolve_type(type_name, from_namespace, type_index)
            if target is None:
                continue
            matches = [c for c in method_index.get(call.name, []) if c.owner_type_id == target.type_id]
            if matches:
                return "Method", _pick_method(matches), {"receiver_type": target.name}
        return None

    # bare or `this` call: prefer the same type's method (implicit this), then a free function.
    if current_type_id is not None:
        same = [c for c in method_index.get(call.name, []) if c.owner_type_id == current_type_id]
        if same:
            return "Method", _pick_method(same), {}
    fns = function_index.get(call.name)
    if fns:
        return "Function", _pick_function(fns, from_namespace), {}
    return None


def _emit_calls_for_type(e: _Entry, methods: list[_MethodRec], type_index,
                         method_index, function_index, externs: set[str]) -> Iterator[GraphNode | GraphEdge]:
    field_types = {f.name: f.type for f in e.raw_type.fields if f.type}
    for r in methods:
        member_types = dict(field_types)
        member_types.update({p.name: p.type for p in r.params if p.name and p.type})
        emitted: set[str] = set()
        for call in r.calls:
            hit = _resolve_call(call, e.namespace, e.type_id, member_types, type_index, method_index, function_index)
            if hit is None:
                yield from _emit_external_call("Method", r.method_id, call, emitted, externs)
                continue
            target_label, target_id, extra = hit
            if target_id in emitted:
                continue
            emitted.add(target_id)
            yield GraphEdge("CALLS", "Method", {"id": r.method_id}, target_label, {"id": target_id},
                            {"resolution": "name", **extra})


def _emit_function_calls(fr: _FuncRec, type_index, method_index, function_index,
                         externs: set[str]) -> Iterator[GraphNode | GraphEdge]:
    member_types = {p.name: p.type for p in fr.raw.params if p.name and p.type}
    emitted: set[str] = set()
    for call in fr.raw.calls:
        hit = _resolve_call(call, fr.namespace, None, member_types, type_index, method_index, function_index)
        if hit is None:
            yield from _emit_external_call("Function", fr.function_id, call, emitted, externs)
            continue
        target_label, target_id, extra = hit
        if target_id in emitted:
            continue
        emitted.add(target_id)
        yield GraphEdge("CALLS", "Function", {"id": fr.function_id}, target_label, {"id": target_id},
                        {"resolution": "name", **extra})


def _emit_external_call(from_label: str, from_id: str, call: RawCall, emitted: set[str],
                        externs: set[str]) -> Iterator[GraphNode | GraphEdge]:
    """An unresolved call the resolver could not bind in-project -> a placeholder Function keyed by
    its cross-repo linkage (std/system or a not-yet-ingested sibling repo), ADR 0004. Only
    free/namespaced calls are nameable this way; constructors / receiver-typed member calls skip."""
    qualified = _external_free_call(call)
    if qualified is None:
        return
    eid = ids.extern_function_key(qualified)
    if eid in emitted:
        return
    emitted.add(eid)
    if eid not in externs:
        externs.add(eid)
        yield _external_function_node(qualified)
    yield GraphEdge("CALLS", from_label, {"id": from_id}, "Function", {"id": eid}, {"resolution": "external"})


# -- C-family: macros + #includes ----------------------------------------------------------

def _emit_macros(raw_files: list[RawFile], project_id: str) -> Iterator[GraphNode | GraphEdge]:
    for rf in raw_files:
        fid = ids.file_id(project_id, rf.path)
        norm = _norm(rf.path)
        for m in rf.macros:
            mid = ids.macro_id(fid, m.name)  # macros are file/preprocessor-scoped (as in C)
            yield GraphNode("Macro", {"id": mid}, {
                "name": m.name, "qualified_name": f"{norm}:{m.name}", "function_like": m.is_function_like,
                "source": m.source, "line_start": m.line_start, "line_end": m.line_end,
            })
            yield GraphEdge("DEFINES", "File", {"id": fid}, "Macro", {"id": mid})


def _include_fullpaths(raw_files: list[RawFile], file_ids: dict[str, str]) -> dict[str, str]:
    """{normalized full path -> File id} -- one entry per file, never ambiguous. The basename shortcuts
    are added separately by `_collapse_basenames`, which must see the WHOLE project at once."""
    return {_norm(rf.path): file_ids[rf.path] for rf in raw_files}


def _collapse_basenames(fullpaths: dict[str, str]) -> dict[str, str]:
    """Build the include-resolution index from a full-path map: keep every full-path key, and add a bare
    basename key ONLY for a GLOBALLY-unique basename (so `#include "x.h"` resolves by basename when the
    project has exactly one `x.h`). The uniqueness count MUST be over the whole project at once -- counting
    within a partition (seed vs emit) would call a basename unique in each half and wrongly resolve an
    #include that the full path leaves dangling (incremental != full)."""
    idx = dict(fullpaths)
    basename_count: dict[str, int] = defaultdict(int)
    for norm in fullpaths:
        basename_count[posixpath.basename(norm)] += 1
    for norm, fid in fullpaths.items():
        base = posixpath.basename(norm)
        if basename_count[base] == 1:
            idx.setdefault(base, fid)
    return idx


def _build_include_index(raw_files: list[RawFile], file_ids: dict[str, str]) -> dict[str, str]:
    """{normalized path AND unique basename -> File id}, so an #include resolves by exact relative path
    first, else by unique basename (mirrors the C ingestor). Used by the full `resolve`, which sees every
    file at once; the incremental path collapses basenames over the seed+emit union instead."""
    return _collapse_basenames(_include_fullpaths(raw_files, file_ids))


def _emit_includes(raw_files: list[RawFile], file_ids: dict[str, str],
                   include_index: dict[str, str]) -> Iterator[GraphEdge]:
    """Resolve each `#include "..."`/`<...>` to a project File via `include_index` (exact relative path,
    else unique basename). The index spans the WHOLE project (seed non-emit files + the emit set on an
    incremental resync), so an emit file's include of an untouched header still resolves -- otherwise the
    INCLUDES edge would vanish on resync (incremental != full). Out-of-project system headers -> no edge."""
    for rf in raw_files:
        fid = file_ids[rf.path]
        for inc in rf.includes:
            target = include_index.get(_norm(inc.path)) or include_index.get(posixpath.basename(inc.path))
            if target is not None and target != fid:
                yield GraphEdge("INCLUDES", "File", {"id": fid}, "File", {"id": target}, {"resolution": "name"})


# -- file provenance (ADR 0005) ------------------------------------------------------------

def _note(prov: dict[str, tuple[str, dict[str, bool]]], entity_id: str, label: str,
          file_id: str, is_def: bool) -> None:
    """Record that `file_id` DEFINES (is_def=True, has a body) or merely DECLARES (False) `entity_id`.
    A file that both declares and inline-defines the same entity counts as a definition (OR of flags)."""
    entry = prov.get(entity_id)
    if entry is None:
        prov[entity_id] = (label, {file_id: is_def})
    else:
        files = entry[1]
        files[file_id] = files.get(file_id, False) or is_def


def _emit_provenance(type_prov, method_prov, func_prov, ns_prov) -> Iterator[GraphEdge]:
    """File-[:DEFINES|DECLARES]->entity for every program-global type/method/function + the namespaces a
    file contributes to -- the per-file ownership handle the authored-shared incremental engine reads and
    diffs (uniform with C/Go/Java). DEFINES where the file carries the body, DECLARES where it only
    declares (a header). Emitted IDENTICALLY by resolve() and resolve_incremental() (shared helper), so a
    full recompute and an incremental resync agree edge-for-edge (the incremental==full invariant)."""
    for prov in (type_prov, method_prov, func_prov):
        for eid, (label, files) in prov.items():
            for fid, is_def in files.items():
                yield GraphEdge("DEFINES" if is_def else "DECLARES", "File", {"id": fid}, label, {"id": eid})
    for nid, fids in ns_prov.items():
        for fid in fids:  # a namespace has no decl/def split -- every contributing file DEFINES it
            yield GraphEdge("DEFINES", "File", {"id": fid}, "Namespace", {"id": nid})


# -- helpers -------------------------------------------------------------------------------

def _method_to_function(md: RawMethod, namespace: str) -> RawFunction:
    return RawFunction(
        name=md.name, namespace=namespace, return_type=md.return_type or "",
        params=list(md.params), is_definition=md.is_definition, calls=list(md.calls),
        signature="", source=md.source, doc=md.doc, line_start=md.line_start, line_end=md.line_end,
    )


def _join_ns(parts: list[str]) -> str:
    return "::".join([p for p in parts if p])


def _norm(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def _basename(path: str) -> str:
    return _norm(path).rsplit("/", 1)[-1]
