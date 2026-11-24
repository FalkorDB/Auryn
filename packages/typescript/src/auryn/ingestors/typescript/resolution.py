"""Project-wide pass: turn the per-file RawFile list into graph nodes/edges, applying the
cross-file decisions a single file can't -- which module an `import` specifier resolves to,
whether a called name is a project function/method, and whether a heritage/type name names a
project-defined type.

Name-based only -- this is the fast (tree-sitter) engine, so every semantic edge is stamped
`resolution="name"`. A future deep tsserver/TS-compiler pass would re-emit the same node ids
with `resolution="resolved"`, upgrading in place (the two-phase model). The Project node is
added by git_sync; everything below hangs off it.

Ambiguity is resolved conservatively: a name that resolves to more than one project
declaration yields no edge (an honest "name" match, not a guess). Receiver typing narrows
method calls -- `store.put()` where `store: Store` prefers `Store.put` over a same-named
method elsewhere."""

from __future__ import annotations

import posixpath
from collections import defaultdict
from dataclasses import dataclass, field

from auryn.core import http_seam
from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.ingestors.typescript import ids
from auryn.ingestors.typescript.raw_model import RawFile, RawMethod

_NAME = {"resolution": "name"}


@dataclass
class _Callable:
    """A function or method whose body carries calls to resolve in the final pass."""

    label: str  # "Function" | "Method"
    node_id: str
    calls: list  # list[RawCall]
    receiver_types: dict[str, str]  # receiver simple name -> bare type name (+ "this")


@dataclass
class _Scope:
    proj_id: str
    module_ids: dict[str, str]  # rf.path -> Module id
    module_by_key: dict[str, str]  # normalized path -> Module id (for import resolution)
    type_index: dict[str, tuple[str, str]]  # unambiguous bare name -> (label, id)
    func_index: dict[str, list[str]]  # function name -> [Function id, ...]
    method_index: dict[str, list[str]]  # method name -> [Method id, ...] (class methods only)
    class_by_name: dict[str, str]  # unambiguous class name -> Class id
    methods_of_class: dict[str, dict[str, str]]  # Class id -> {method name -> Method id}
    callables: list = field(default_factory=list)  # _Callable, filled during emission
    emitted_externs: set = field(default_factory=set)  # placeholder ids emitted this pass (ADR 0004)


@dataclass
class GraphIndex:
    """The subset of `_Scope` reconstructable from the graph for modules NOT being re-emitted
    -- the raw (name, id) symbol contributions of every other file. Seeds `resolve_incremental`
    so an incremental resync can emit only the changed+affected modules while still resolving
    their cross-module references. See core/incremental_scoped.py and ADR 0002."""

    module_by_key: dict[str, str] = field(default_factory=dict)  # normalized path -> Module id
    types: list = field(default_factory=list)      # (name, label, id) for Class/Interface/Enum/TypeAlias
    functions: list = field(default_factory=list)  # (name, id)
    classes: list = field(default_factory=list)    # (name, class_id)
    methods: list = field(default_factory=list)    # (class_id, method_name, method_id)


def resolve(files: list[RawFile], proj_id: str) -> list[GraphNode | GraphEdge]:
    module_ids = {rf.path: ids.module_id(proj_id, rf.path) for rf in files}
    items = _emit_modules(files, module_ids, proj_id)
    scope = _build_scope(files, module_ids, proj_id)
    _emit_all(files, scope, items)
    return items


def resolve_incremental(emit_files: list[RawFile], proj_id: str, seed: GraphIndex) -> list[GraphNode | GraphEdge]:
    """Resolve+emit ONLY `emit_files` (the changed+affected set), binding their references
    against a symbol table = these files' own symbols merged over `seed` (every OTHER module's
    symbols, read back from the graph). Output is identical to `resolve`'s for these files --
    verified by the equivalence oracle (tests/ingestors/test_typescript_incremental_equivalence)."""
    module_ids = {rf.path: ids.module_id(proj_id, rf.path) for rf in emit_files}
    items = _emit_modules(emit_files, module_ids, proj_id)
    scope = _build_scope(emit_files, module_ids, proj_id, seed)
    _emit_all(emit_files, scope, items)
    return items


def _emit_modules(files, module_ids, proj_id) -> list:
    items: list[GraphNode | GraphEdge] = []
    for rf in files:
        mid = module_ids[rf.path]
        norm = _norm(rf.path)
        items.append(GraphNode("Module", {"id": mid}, {
            "path": norm, "name": posixpath.basename(norm),
            "dialect": rf.dialect, "is_declaration": rf.is_declaration,
        }))
        items.append(GraphEdge("HAS_MODULE", "Project", {"id": proj_id}, "Module", {"id": mid}))
    return items


def _emit_all(files, scope, items) -> None:
    _emit_classes(files, scope, items)
    _emit_interfaces(files, scope, items)
    _emit_enums(files, scope, items)
    _emit_type_aliases(files, scope, items)
    _emit_functions(files, scope, items)
    _register_module_calls(files, scope)
    _emit_imports(files, scope, items)
    _emit_calls(scope, items)


# -- indices ------------------------------------------------------------------------------

def _build_scope(files, module_ids, proj_id, seed: GraphIndex | None = None) -> _Scope:
    type_index: dict[str, list[tuple[str, str]]] = defaultdict(list)
    func_index: dict[str, list[str]] = defaultdict(list)
    method_index: dict[str, list[str]] = defaultdict(list)
    class_by_name: dict[str, list[str]] = defaultdict(list)
    methods_of_class: dict[str, dict[str, str]] = {}
    module_by_key: dict[str, str] = {}

    if seed is not None:  # every OTHER module's symbols, read back from the graph
        for name, label, nid in seed.types:
            type_index[name].append((label, nid))
        for name, nid in seed.functions:
            func_index[name].append(nid)
        for name, cid in seed.classes:
            class_by_name[name].append(cid)
        for cid, mname, mid in seed.methods:
            methods_of_class.setdefault(cid, {})[mname] = mid
            method_index[mname].append(mid)
        module_by_key.update(seed.module_by_key)

    for rf in files:
        _index_file(rf, module_ids[rf.path], type_index, func_index, method_index, class_by_name, methods_of_class)
    module_by_key.update({_np(rf.path): module_ids[rf.path] for rf in files})

    return _Scope(
        proj_id=proj_id,
        module_ids=module_ids,
        module_by_key=module_by_key,
        # keep only unambiguous names for edge targets -- an honest name-based match
        type_index={n: v[0] for n, v in type_index.items() if len(v) == 1},
        func_index=dict(func_index),
        method_index=dict(method_index),
        class_by_name={n: v[0] for n, v in class_by_name.items() if len(v) == 1},
        methods_of_class=methods_of_class,
    )


def _index_file(rf, mid, type_index, func_index, method_index, class_by_name, methods_of_class) -> None:
    """One file's raw (name -> id) contributions to the cross-file scope tables -- the per-file
    body of _build_scope, factored out so build_plan (streaming) can call it while loading each
    cached file one at a time."""
    for c in rf.classes:
        cid = ids.class_id(mid, c.name)
        type_index[c.name].append(("Class", cid))
        class_by_name[c.name].append(cid)
        mmap = methods_of_class.setdefault(cid, {})
        for m in c.methods:
            meth_id = ids.method_id(cid, m.name, _param_names(m))
            mmap[m.name] = meth_id
            method_index[m.name].append(meth_id)
    for it in rf.interfaces:
        type_index[it.name].append(("Interface", ids.interface_id(mid, it.name)))
    for en in rf.enums:
        type_index[en.name].append(("Enum", ids.enum_id(mid, en.name)))
    for ta in rf.type_aliases:
        type_index[ta.name].append(("TypeAlias", ids.type_alias_id(mid, ta.name)))
    for fn in rf.functions:
        func_index[fn.name].append(ids.function_id(mid, fn.name, _param_names(fn)))


# -- streaming resolution (memory-bounded: source stays on disk, loaded per file) ----------
#
# resolve() above holds every parsed RawFile (all declaration source text) in memory at once.
# For a large first ingest that dominates peak RSS. build_plan() instead scans each cached
# file once to build only the lightweight cross-file scope (name -> id tables -- no source),
# and emit_streaming() then emits the SAME nodes/edges as resolve() but loads only one file at
# a time. Peak memory is O(scope + one file), independent of corpus size. TS has no cross-file
# symbol merge (each declaration lives in one file), so emission is simply per file. Driven by
# git_sync over the blob-sha parse cache.


@dataclass
class StreamPlan:
    scope: _Scope
    file_refs: list  # [(path, blob_sha)]
    broken: list     # paths whose parse hit a syntax error (rf.has_error)


def build_plan(file_refs, load_fn, proj_id: str) -> StreamPlan:
    """Phase 1: scan each cached file once into the cross-file _Scope (name -> id tables only),
    dropping source. `file_refs` is (path, blob_sha) pairs; `load_fn(blob_sha)` returns a fresh
    RawFile. Module ids come from paths alone (no load needed there). Also collects the broken
    (has_error) paths here, since git_sync no longer holds the file list to derive them from."""
    file_refs = list(file_refs)
    module_ids = {path: ids.module_id(proj_id, path) for path, _sha in file_refs}
    type_index: dict[str, list[tuple[str, str]]] = defaultdict(list)
    func_index: dict[str, list[str]] = defaultdict(list)
    method_index: dict[str, list[str]] = defaultdict(list)
    class_by_name: dict[str, list[str]] = defaultdict(list)
    methods_of_class: dict[str, dict[str, str]] = {}
    broken: list[str] = []
    for path, sha in file_refs:
        rf = load_fn(sha)
        rf.path = path
        if rf.has_error:
            broken.append(path)
        _index_file(rf, module_ids[path], type_index, func_index, method_index, class_by_name, methods_of_class)
        del rf
    scope = _Scope(
        proj_id=proj_id,
        module_ids=module_ids,
        module_by_key={_np(path): module_ids[path] for path, _ in file_refs},
        type_index={n: v[0] for n, v in type_index.items() if len(v) == 1},
        func_index=dict(func_index),
        method_index=dict(method_index),
        class_by_name={n: v[0] for n, v in class_by_name.items() if len(v) == 1},
        methods_of_class=methods_of_class,
    )
    return StreamPlan(scope=scope, file_refs=file_refs, broken=sorted(broken))


def emit_streaming(plan: StreamPlan, load_fn, proj_id: str):
    """Phase 2: yield the same nodes/edges as resolve(), loading one file at a time. Reuses the
    per-file emitters unchanged by giving each a small local buffer (its own file's items), so
    nothing but the current file and the scope is resident. Safe to iterate repeatedly (each
    call reloads; nothing retained) -- stream_apply iterates it twice.

    `scope.callables` is reset per file so _emit_calls emits only that file's calls (resolved
    against the complete global scope); `scope.emitted_externs` is reset once per call so the
    placeholder-Module dedup spans files within a pass -- matching resolve()'s single-pass
    behaviour. A cross-file edge (CALLS/EXTENDS/IMPORTS to a symbol in another file) is still
    written only once every node exists, because stream_apply writes all nodes before any edge."""
    scope = plan.scope
    scope.emitted_externs = set()
    for path, sha in plan.file_refs:
        rf = load_fn(sha)
        rf.path = path
        scope.callables = []
        buf = _emit_modules([rf], scope.module_ids, proj_id)
        _emit_classes([rf], scope, buf)
        _emit_interfaces([rf], scope, buf)
        _emit_enums([rf], scope, buf)
        _emit_type_aliases([rf], scope, buf)
        _emit_functions([rf], scope, buf)
        _register_module_calls([rf], scope)
        _emit_imports([rf], scope, buf)
        _emit_calls(scope, buf)
        yield from buf
        del rf, buf


def callable_locations(files, proj_id: str):
    """Every project callable's SCIP-join location (graph id, file, 1-based line span, name, called
    names) -- the bridge the deep (scip-typescript / SCIP) pass needs. Reuses the SAME id builders as
    the fast pass (module/function/class/interface ids + method ids embed param names via _param_names),
    so deep-pass CALLS edges built from these land byte-identically on the nodes the fast pass authored
    -- no type inference here. `files` is an iterable of RawFile with `.path` set (the deep pass streams
    them from the parse cache, re-stamping path exactly as build_plan/emit_streaming do); each RawFile is
    dropped after its locations are extracted, so only the small location list stays resident. Interface
    methods are included -- they have no body (no calls) but can be deep-resolved call TARGETS. The
    core.scip_resolve import is deferred to here (deep-only) so the fast pass never requires it -- the fast
    graph stays buildable even against an older auryn-core without scip_resolve."""
    from auryn.core.scip_resolve import CallableLocation

    out: list = []
    for rf in files:
        mid = ids.module_id(proj_id, rf.path)
        rel = _norm(rf.path)
        for fn in rf.functions:
            out.append(CallableLocation(
                ids.function_id(mid, fn.name, _param_names(fn)), "Function", rel,
                fn.line_start, fn.line_end, fn.name, frozenset(c.name for c in fn.calls),
                frozenset((c.name, c.line) for c in fn.calls),
                frozenset((c.line, c.col, c.name) for c in fn.calls)))
        for c in rf.classes:
            cid = ids.class_id(mid, c.name)
            for m in c.methods:
                out.append(CallableLocation(
                    ids.method_id(cid, m.name, _param_names(m)), "Method", rel,
                    m.line_start, m.line_end, m.name, frozenset(cc.name for cc in m.calls),
                    frozenset((cc.name, cc.line) for cc in m.calls),
                    frozenset((cc.line, cc.col, cc.name) for cc in m.calls)))
        for it in rf.interfaces:
            iid = ids.interface_id(mid, it.name)
            for m in it.methods:
                out.append(CallableLocation(
                    ids.method_id(iid, m.name, _param_names(m)), "Method", rel,
                    m.line_start, m.line_end, m.name, frozenset()))
    return out


# -- emitters -----------------------------------------------------------------------------

def _emit_classes(files, scope: _Scope, items: list) -> None:
    for rf in files:
        mid = scope.module_ids[rf.path]
        for c in rf.classes:
            cid = ids.class_id(mid, c.name)
            items.append(GraphNode("Class", {"id": cid}, {
                "name": c.name, "qualified_name": f"{_norm(rf.path)}:{c.name}",
                "linkage_key": ids.extern_symbol_key(_norm(rf.path), c.name),  # ADR 0004
                "is_abstract": c.is_abstract, "is_exported": c.is_exported,
                "type_parameters": c.type_parameters, "source": c.source, "doc": c.doc,
                **_loc(rf.path, c.line_start, c.line_end),
            }))
            items.append(GraphEdge("HAS_TYPE", "Module", {"id": mid}, "Class", {"id": cid}))
            _emit_heritage(c, cid, "Class", scope, items)
            _emit_properties(c.properties, cid, "Class", rf, scope, items)
            for m in c.methods:
                _emit_method(m, cid, c.name, rf, scope, items)


def _emit_interfaces(files, scope: _Scope, items: list) -> None:
    for rf in files:
        mid = scope.module_ids[rf.path]
        for it in rf.interfaces:
            iid = ids.interface_id(mid, it.name)
            items.append(GraphNode("Interface", {"id": iid}, {
                "name": it.name, "qualified_name": f"{_norm(rf.path)}:{it.name}",
                "linkage_key": ids.extern_symbol_key(_norm(rf.path), it.name),  # ADR 0004
                "is_exported": it.is_exported, "type_parameters": it.type_parameters,
                "source": it.source, "doc": it.doc, **_loc(rf.path, it.line_start, it.line_end),
            }))
            items.append(GraphEdge("HAS_TYPE", "Module", {"id": mid}, "Interface", {"id": iid}))
            _emit_heritage(it, iid, "Interface", scope, items)
            _emit_properties(it.properties, iid, "Interface", rf, scope, items)
            for m in it.methods:  # method_signature -- no body, so no CALLS
                _emit_method(m, iid, it.name, rf, scope, items, owner_is_interface=True)


def _emit_enums(files, scope: _Scope, items: list) -> None:
    for rf in files:
        mid = scope.module_ids[rf.path]
        for en in rf.enums:
            eid = ids.enum_id(mid, en.name)
            items.append(GraphNode("Enum", {"id": eid}, {
                "name": en.name, "qualified_name": f"{_norm(rf.path)}:{en.name}",
                "is_const": en.is_const, "is_exported": en.is_exported, "members": en.members,
                "source": en.source, "doc": en.doc, **_loc(rf.path, en.line_start, en.line_end),
            }))
            items.append(GraphEdge("HAS_TYPE", "Module", {"id": mid}, "Enum", {"id": eid}))
            for pos, member in enumerate(en.members):
                memid = ids.enum_member_id(eid, member)
                items.append(GraphNode("EnumMember", {"id": memid}, {"name": member, "position": pos}))
                items.append(GraphEdge("HAS_ENUM_MEMBER", "Enum", {"id": eid}, "EnumMember", {"id": memid}))


def _emit_type_aliases(files, scope: _Scope, items: list) -> None:
    for rf in files:
        mid = scope.module_ids[rf.path]
        for ta in rf.type_aliases:
            tid = ids.type_alias_id(mid, ta.name)
            items.append(GraphNode("TypeAlias", {"id": tid}, {
                "name": ta.name, "qualified_name": f"{_norm(rf.path)}:{ta.name}",
                "value": ta.value, "is_exported": ta.is_exported, "source": ta.source, "doc": ta.doc,
                **_loc(rf.path, ta.line_start, ta.line_end),
            }))
            items.append(GraphEdge("HAS_TYPE", "Module", {"id": mid}, "TypeAlias", {"id": tid}))
            # a type alias referencing project types (`type X = A | B`)
            for ref in _dedup(ta.type_refs):
                hit = scope.type_index.get(ref)
                if hit is not None and hit[1] != tid:
                    items.append(GraphEdge("REFERENCES", "TypeAlias", {"id": tid}, hit[0], {"id": hit[1]}, _NAME))


def _emit_functions(files, scope: _Scope, items: list) -> None:
    for rf in files:
        mid = scope.module_ids[rf.path]
        for fn in rf.functions:
            fid = ids.function_id(mid, fn.name, _param_names(fn))
            items.append(GraphNode("Function", {"id": fid}, {
                "name": fn.name, "qualified_name": f"{_norm(rf.path)}:{fn.name}",
                "linkage_key": ids.extern_symbol_key(_norm(rf.path), fn.name),  # ADR 0004
                "kind": fn.kind, "is_async": fn.is_async, "is_exported": fn.is_exported,
                "return_type": fn.return_type, "signature": _signature(fn.name, fn),
                "source": fn.source, "doc": fn.doc, **_loc(rf.path, fn.line_start, fn.line_end),
            }))
            items.append(GraphEdge("HAS_FUNCTION", "Module", {"id": mid}, "Function", {"id": fid}))
            _emit_parameters(fn, fid, "Function", items)
            _emit_type_edges(fn, fid, "Function", scope, items)
            scope.callables.append(_Callable("Function", fid, fn.calls, _receiver_types(fn, None)))


def _register_module_calls(files, scope: _Scope) -> None:
    """Module-scope code (test `describe`/`it` callbacks, `createRoot(..).render(<App />)`, route tables)
    has no enclosing callable, so the Module itself is the CALLS source -- as in the Python ingestor.
    Modules are not deep-pass callables (`callable_locations`), so these fast edges are never retired."""
    for rf in files:
        if rf.module_calls:
            scope.callables.append(_Callable("Module", scope.module_ids[rf.path], rf.module_calls, {}))


# Node.js built-in modules (and their `node:` forms) -> origin "stdlib"; any other bare specifier
# is a third-party/sibling-repo package -> "external".
_NODE_BUILTINS = frozenset({
    "fs", "path", "os", "http", "https", "http2", "net", "url", "util", "events", "stream",
    "crypto", "zlib", "buffer", "child_process", "cluster", "dns", "assert", "process", "querystring",
    "readline", "repl", "tls", "tty", "dgram", "vm", "worker_threads", "perf_hooks", "async_hooks",
    "string_decoder", "timers", "console", "module", "constants",
})


def _ts_import_origin(specifier: str) -> str:
    head = specifier[5:] if specifier.startswith("node:") else specifier
    head = head.split("/", 1)[0]
    return "stdlib" if head in _NODE_BUILTINS else "external"


def _emit_imports(files, scope: _Scope, items: list) -> None:
    for rf in files:
        mid = scope.module_ids[rf.path]
        seen: set[str] = set()
        for imp in rf.imports:
            target = _resolve_import(imp.module, rf.path, scope.module_by_key)
            if target is not None:
                if target != mid and target not in seen:
                    seen.add(target)
                    items.append(GraphEdge("IMPORTS", "Module", {"id": mid}, "Module", {"id": target},
                                           {**_NAME, "kind": imp.kind}))
            elif not imp.module.startswith((".", "/")):
                # A bare specifier: an npm package or another repo, never a project-relative module
                # -> a placeholder Module keeps the dependency visible (ADR 0004). Unresolved
                # *relative* paths are left dropped (they may become project modules later, which
                # would strand a placeholder across an incremental resync).
                eid = ids.extern_module_key(imp.module)
                if eid not in seen:
                    seen.add(eid)
                    if eid not in scope.emitted_externs:
                        scope.emitted_externs.add(eid)
                        items.append(GraphNode("Module", {"id": eid}, {
                            "name": imp.module, "path": imp.module,
                            "resolved": False, "origin": _ts_import_origin(imp.module), "linkage_key": eid,
                        }))
                    items.append(GraphEdge("IMPORTS", "Module", {"id": mid}, "Module", {"id": eid},
                                           {"resolution": "external", "kind": imp.kind}))


def _emit_http_call(call_owner, call, http_seen: set, items: list) -> None:
    """ADR 0006: materialize an HTTP-client call as `caller -CALLS_API-> APIEndpoint{resolved:false}`,
    a route-keyed placeholder every caller of that route+verb converges on; the HTTP stitch hook
    later resolves it to the handler across repos. Keying via core.http_seam so C# and TS agree."""
    route = http_seam.normalize_route(call.http_url)
    if not route:
        return
    method = (call.http_method or "ANY").upper()
    endpoint_id = http_seam.endpoint_placeholder_key(method, route)
    if endpoint_id not in http_seen:
        http_seen.add(endpoint_id)
        items.append(GraphNode("APIEndpoint", {"id": endpoint_id},
                               {"name": f"{method} {route}", "route": route, "http_method": method,
                                "resolved": False, "provenance": "http-derived"}))
    items.append(GraphEdge("CALLS_API", call_owner.label, {"id": call_owner.node_id},
                           "APIEndpoint", {"id": endpoint_id}, {"resolution": "name", "provenance": "http-derived"}))


def _emit_calls(scope: _Scope, items: list) -> None:
    http_seen: set = set()
    for call_owner in scope.callables:
        emitted: set[tuple] = set()
        for call in call_owner.calls:
            if call.http_url:
                _emit_http_call(call_owner, call, http_seen, items)
            hit = _resolve_call(call, call_owner.receiver_types, scope)
            if hit is None:
                continue
            label, target, extra = hit
            key = (label, target)
            if key in emitted:
                continue
            emitted.add(key)
            items.append(GraphEdge("CALLS", call_owner.label, {"id": call_owner.node_id},
                                   label, {"id": target}, {**_NAME, **extra}))


# -- shared member emission ---------------------------------------------------------------

def _emit_heritage(decl, owner_id: str, owner_label: str, scope: _Scope, items: list) -> None:
    for base in decl.extends:
        hit = scope.type_index.get(base)
        if hit is not None and hit[1] != owner_id:
            # class extends class; interface extends interface
            items.append(GraphEdge("EXTENDS", owner_label, {"id": owner_id}, hit[0], {"id": hit[1]}, _NAME))
    implements = getattr(decl, "implements", [])
    for iface in implements:
        hit = scope.type_index.get(iface)
        if hit is not None and hit[0] == "Interface":
            items.append(GraphEdge("IMPLEMENTS", owner_label, {"id": owner_id}, "Interface", {"id": hit[1]}, _NAME))


def _emit_properties(properties, owner_id: str, owner_label: str, rf, scope: _Scope, items: list) -> None:
    ref_targets: set[tuple[str, str]] = set()
    for p in properties:
        pid = ids.property_id(owner_id, p.name)
        items.append(GraphNode("Property", {"id": pid}, {
            "name": p.name, "type": p.type, "accessibility": p.accessibility,
            "is_static": p.is_static, "is_readonly": p.is_readonly, "is_optional": p.is_optional,
        }))
        items.append(GraphEdge("HAS_PROPERTY", owner_label, {"id": owner_id}, "Property", {"id": pid}))
        for ref in p.type_refs:
            hit = scope.type_index.get(ref)
            if hit is not None and hit[1] != owner_id:
                ref_targets.add((hit[0], hit[1]))
    for label, tid in ref_targets:  # owner REFERENCES the property types (as C fields do)
        items.append(GraphEdge("REFERENCES", owner_label, {"id": owner_id}, label, {"id": tid}, _NAME))


def _emit_method(m: RawMethod, owner_id: str, owner_name: str, rf, scope: _Scope, items: list,
                 owner_is_interface: bool = False) -> None:
    meth_id = ids.method_id(owner_id, m.name, _param_names(m))
    items.append(GraphNode("Method", {"id": meth_id}, {
        "name": m.name, "qualified_name": f"{owner_name}.{m.name}",
        "linkage_key": ids.extern_symbol_key(_norm(rf.path), f"{owner_name}.{m.name}"),  # ADR 0004
        "kind": m.kind,
        "accessibility": m.accessibility, "is_static": m.is_static, "is_abstract": m.is_abstract,
        "is_async": m.is_async, "return_type": m.return_type, "signature": _signature(m.name, m),
        "source": m.source, "doc": m.doc, **_loc(rf.path, m.line_start, m.line_end),
    }))
    items.append(GraphEdge("HAS_METHOD", "Class" if not owner_is_interface else "Interface",
                           {"id": owner_id}, "Method", {"id": meth_id}))
    _emit_parameters(m, meth_id, "Method", items)
    _emit_type_edges(m, meth_id, "Method", scope, items)
    if not owner_is_interface:  # interface methods have no body -> no calls
        scope.callables.append(_Callable("Method", meth_id, m.calls, _receiver_types(m, owner_name)))


def _emit_parameters(callable_obj, owner_id: str, owner_label: str, items: list) -> None:
    for p in callable_obj.params:
        pname = p.name or f"arg{p.position}"
        pid = ids.parameter_id(owner_id, p.position, pname)
        items.append(GraphNode("Parameter", {"id": pid}, {
            "name": pname, "type": p.type, "position": p.position,
            "optional": p.optional, "is_rest": p.is_rest,
        }))
        items.append(GraphEdge("HAS_PARAMETER", owner_label, {"id": owner_id}, "Parameter", {"id": pid}))


def _emit_type_edges(callable_obj, owner_id: str, owner_label: str, scope: _Scope, items: list) -> None:
    """RETURNS for the return type; REFERENCES for parameter types -- both to project types
    only, name-resolved."""
    for ref in _dedup(callable_obj.return_type_refs):
        hit = scope.type_index.get(ref)
        if hit is not None and hit[1] != owner_id:
            items.append(GraphEdge("RETURNS", owner_label, {"id": owner_id}, hit[0], {"id": hit[1]}, _NAME))
    param_targets: set[tuple[str, str]] = set()
    for p in callable_obj.params:
        for ref in p.type_refs:
            hit = scope.type_index.get(ref)
            if hit is not None and hit[1] != owner_id:
                param_targets.add((hit[0], hit[1]))
    for label, tid in param_targets:
        items.append(GraphEdge("REFERENCES", owner_label, {"id": owner_id}, label, {"id": tid}, _NAME))


# -- call resolution ----------------------------------------------------------------------

def _resolve_call(call, receiver_types: dict[str, str], scope: _Scope):
    """(target_label, target_id, extra_props) or None. `new X()` -> X's constructor;
    `foo()` -> a unique project function; `recv.m()` -> a method, preferring the receiver's
    declared type, else a unique method name. A JSX element is resolved by `_resolve_jsx`."""
    if call.is_jsx:
        return _resolve_jsx(call, scope)
    if call.is_new:
        cid = scope.class_by_name.get(call.name)
        if cid is not None:
            ctor = scope.methods_of_class.get(cid, {}).get("constructor")
            if ctor is not None:
                return ("Method", ctor, {})
        return None
    if call.receiver is None:
        hits = scope.func_index.get(call.name)
        if hits and len(hits) == 1:
            return ("Function", hits[0], {})
        return None
    rtype = receiver_types.get(call.receiver)
    if rtype is not None:
        cid = scope.class_by_name.get(rtype)
        if cid is not None:
            m = scope.methods_of_class.get(cid, {}).get(call.name)
            if m is not None:
                return ("Method", m, {"receiver_type": rtype})
    hits = scope.method_index.get(call.name)
    if hits and len(hits) == 1:
        return ("Method", hits[0], {})
    return None


def _resolve_jsx(call, scope: _Scope):
    """`<Card />` -> a unique project function component, else a unique class component's `render`
    (React invokes it, as `new X()` targets X's constructor). A member tag (`<Tabs.Panel>`) names a
    property of a value, not a class method, so the fast pass leaves it to the deep pass rather than
    guess a same-named method."""
    if call.receiver is not None:
        return None
    hits = scope.func_index.get(call.name)
    if hits and len(hits) == 1:
        return ("Function", hits[0], {})
    cid = scope.class_by_name.get(call.name)
    if cid is not None:
        render = scope.methods_of_class.get(cid, {}).get("render")
        if render is not None:
            return ("Method", render, {})
    return None


def _receiver_types(callable_obj, enclosing_class: str | None) -> dict[str, str]:
    """Map a receiver simple name to its bare declared type: each parameter by its first type
    ref, plus `this` -> the enclosing class (methods only)."""
    out: dict[str, str] = {}
    if enclosing_class is not None:
        out["this"] = enclosing_class
    for p in callable_obj.params:
        if p.name and p.type_refs:
            out[p.name] = p.type_refs[0]
    return out


# -- module resolution --------------------------------------------------------------------

# TS/Node resolution order for an extensionless relative specifier: try the path with each
# source extension, then as a directory with an index file. `.d.ts` sits alongside `.ts`.
_RESOLVE_EXTS = (".ts", ".tsx", ".d.ts", ".js", ".jsx", ".mjs", ".cjs")


def _resolve_import(specifier: str, from_path: str, module_by_key: dict[str, str]) -> str | None:
    """Resolve a relative import specifier to a project Module id, or None for a bare/package
    specifier (`react`, `lodash`) or an unresolved path. Bare specifiers are external -- the
    front-end tier's value is the *intra*-project module graph, so we don't invent nodes for
    node_modules."""
    if not specifier.startswith("."):
        return None
    base_dir = posixpath.dirname(_np(from_path))
    target = _np(posixpath.join(base_dir, specifier))
    # exact (specifier already carried an extension), then extension probes, then index files
    for cand in (target, *(target + e for e in _RESOLVE_EXTS),
                 *(_np(posixpath.join(target, "index" + e)) for e in _RESOLVE_EXTS)):
        hit = module_by_key.get(cand)
        if hit is not None:
            return hit
    return None


# -- helpers ------------------------------------------------------------------------------

def _norm(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def _np(path: str) -> str:
    return posixpath.normpath(path.replace("\\", "/"))


def _loc(file_path: str, line_start: int, line_end: int) -> dict:
    return {"file_path": _norm(file_path), "line_start": line_start, "line_end": line_end}


def _param_names(callable_obj) -> list[str]:
    return [p.name or f"arg{p.position}" for p in callable_obj.params]


def _signature(name: str, callable_obj) -> str:
    parts = []
    for p in callable_obj.params:
        pre = "..." if p.is_rest else ""
        nm = (p.name or f"arg{p.position}") + ("?" if p.optional and not p.is_rest else "")
        parts.append((pre + nm + (": " + p.type if p.type else "")).strip())
    sig = f"{name}({', '.join(parts)})"
    return sig + (": " + callable_obj.return_type if callable_obj.return_type else "")


def _dedup(seq) -> list[str]:
    seen: set[str] = set()
    return [x for x in seq if not (x in seen or seen.add(x))]
