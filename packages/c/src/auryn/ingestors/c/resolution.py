"""Project-wide pass: turn the per-file RawFile list into graph nodes/edges, applying C's
linkage rules that a single file can't (which `foo()` call is the extern vs a file-local
static, whether an `#include` resolves inside the project, one Function entity shared by the
header that declares it and the .c that defines it).

Name-based only -- this is the fast (tree-sitter) engine, so every semantic edge is stamped
`resolution="name"`. A future libclang deep pass would re-emit the same node ids with
`resolution="resolved"`, upgrading in place (the two-phase model)."""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field

from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.ingestors.c import ids
from auryn.ingestors.c.raw_model import RawFile, RawFunction

_TYPE_PRECEDENCE = {"Typedef": 3, "Struct": 2, "Union": 1, "Enum": 0}

# ADR 0004: a curated C standard-library / POSIX name set, so a call to one is surfaced with
# origin="libc" (a terminal external that never resolves to an ingested repo) rather than
# "external" (a candidate a sibling repo could define). Not exhaustive -- covers the common
# allocator/string/stdio/process/socket/thread surface that matters for audit queries.
_LIBC = frozenset({
    "malloc", "calloc", "realloc", "free", "aligned_alloc",
    "memcpy", "memmove", "memset", "memcmp", "memchr",
    "strcpy", "strncpy", "strcat", "strncat", "strcmp", "strncmp", "strlen", "strnlen",
    "strchr", "strrchr", "strstr", "strdup", "strndup", "strtok", "strerror",
    "printf", "fprintf", "sprintf", "snprintf", "vprintf", "vfprintf", "vsnprintf",
    "scanf", "fscanf", "sscanf", "puts", "fputs", "fgets", "getchar", "putchar",
    "fopen", "fdopen", "freopen", "fclose", "fread", "fwrite", "fseek", "ftell", "rewind",
    "fflush", "feof", "ferror", "perror", "setvbuf",
    "exit", "_exit", "abort", "atexit", "system", "getenv", "setenv",
    "qsort", "bsearch", "abs", "labs", "atoi", "atol", "atoll", "strtol", "strtoul", "strtod",
    "rand", "srand", "time", "clock", "gettimeofday", "localtime", "gmtime", "strftime",
    "assert", "longjmp", "setjmp", "va_start", "va_end", "va_arg",
    "open", "close", "read", "write", "lseek", "pread", "pwrite", "unlink", "stat", "fstat",
    "fork", "vfork", "execl", "execlp", "execle", "execv", "execvp", "execve",
    "wait", "waitpid", "pipe", "dup", "dup2", "kill", "signal", "sigaction", "raise",
    "getpid", "getppid", "getuid", "geteuid", "sleep", "usleep", "nanosleep", "alarm",
    "socket", "bind", "listen", "accept", "connect", "send", "recv", "sendto", "recvfrom",
    "setsockopt", "getsockopt", "select", "poll", "shutdown", "gethostbyname",
    "mmap", "munmap", "mprotect", "brk", "sbrk",
    "pthread_create", "pthread_join", "pthread_detach", "pthread_mutex_init",
    "pthread_mutex_lock", "pthread_mutex_unlock", "pthread_mutex_destroy",
    "pthread_cond_wait", "pthread_cond_signal", "pthread_cond_broadcast",
})


def _c_origin(name: str) -> str:
    return "libc" if name in _LIBC else "external"


@dataclass
class _Scope:
    proj_id: str
    file_ids: dict[str, str]  # rel_path -> File id
    extern_funcs: dict[str, str]  # name -> Function id (external linkage)
    type_index: dict[str, tuple[str, str]]  # bare name -> (label, id) for REFERENCES
    include_index: dict[str, str]  # normalized path & basename -> File id
    emitted_externs: set[str]  # placeholder ids already emitted this pass (dedup, ADR 0004)
    # Names defined by a NON-emit file (empty for a full resolve). A declaration in the emit set for
    # one of these emits only its DECLARES edge, not a node (the real definition is untouched).
    defined_elsewhere_funcs: frozenset = frozenset()
    defined_elsewhere_types: frozenset = frozenset()


@dataclass
class CGraphIndex:
    """Resolution symbol tables seeded from the GRAPH for the NON-emit files (ADR 0005), so
    `resolve_incremental` can resolve an emit file's cross-file calls/refs/includes without
    re-parsing the whole project. Reconstructed by `c.incremental.read_c_index`."""

    extern_funcs: dict = field(default_factory=dict)   # name -> external Function id
    type_index: dict = field(default_factory=dict)     # bare name -> (label, id)
    include_index: dict = field(default_factory=dict)  # normalized path & unique basename -> File id
    # Names DEFINED by a non-emit file. A prototype/forward-decl in an EMIT file for one of these
    # emits only its DECLARES edge, never a (sparse) node -- the real definition lives in an untouched
    # file whose node must not be overwritten (ADR 0005 definition-vs-declaration ownership).
    defined_func_names: set = field(default_factory=set)
    defined_type_names: set = field(default_factory=set)


def resolve(files: list[RawFile], proj_id: str) -> list[GraphNode | GraphEdge]:
    items: list[GraphNode | GraphEdge] = []

    # Process files in a stable path order so a symbol DEFINED in several files (multi-program dirs:
    # many `main`, several `struct config`) reduces to an order-INDEPENDENT winner -- identical here
    # and in resolve_incremental's subset, the precondition for incremental == full (ADR 0005).
    files = sorted(files, key=lambda rf: _norm(rf.path))
    file_ids = {rf.path: ids.file_id(proj_id, rf.path) for rf in files}
    for rf in files:
        norm = _norm(rf.path)
        items.append(GraphNode("File", {"id": file_ids[rf.path]}, {"path": norm, "name": posixpath.basename(norm)}))

    scope = _Scope(
        proj_id=proj_id,
        file_ids=file_ids,
        extern_funcs=_extern_func_index(files, proj_id),
        type_index=_type_index(files, proj_id),
        include_index=_include_index(files, file_ids),
        emitted_externs=set(),
    )

    _emit_types(files, proj_id, items)
    _emit_globals_and_macros(files, proj_id, items)
    _emit_functions(files, scope, items)
    _emit_includes(files, scope, items)
    return items


def resolve_incremental(emit_raws: list[RawFile], proj_id: str, index: CGraphIndex) -> list:
    """Emit graph items for ONLY `emit_raws`, resolving cross-file references against `index`
    (graph-seeded non-emit symbols) merged with the emit files' own symbols. The blast-radius
    analogue of `resolve()` (ADR 0005) -- same emitters, scope seeded rather than whole-project.
    The engine's co-contributor closure guarantees every shared node an emit file touches is
    fully re-authored here, so a merged prototype/definition never loses to a sparse re-emit."""
    items: list = []
    emit_raws = sorted(emit_raws, key=lambda rf: _norm(rf.path))  # same stable order as resolve() (winner determinism)
    file_ids = {rf.path: ids.file_id(proj_id, rf.path) for rf in emit_raws}
    for rf in emit_raws:
        norm = _norm(rf.path)
        items.append(GraphNode("File", {"id": file_ids[rf.path]}, {"path": norm, "name": posixpath.basename(norm)}))

    extern_funcs = {**index.extern_funcs, **_extern_func_index(emit_raws, proj_id)}
    type_index = dict(index.type_index)
    for name, (label, tid) in _type_index(emit_raws, proj_id).items():
        cur = type_index.get(name)
        if cur is None or _TYPE_PRECEDENCE[label] >= _TYPE_PRECEDENCE[cur[0]]:
            type_index[name] = (label, tid)
    include_index = {**index.include_index, **_include_index(emit_raws, file_ids)}

    scope = _Scope(proj_id, file_ids, extern_funcs, type_index, include_index, set(),
                   defined_elsewhere_funcs=frozenset(index.defined_func_names),
                   defined_elsewhere_types=frozenset(index.defined_type_names))
    _emit_types(emit_raws, proj_id, items, frozenset(index.defined_type_names))
    _emit_globals_and_macros(emit_raws, proj_id, items)
    _emit_functions(emit_raws, scope, items)
    _emit_includes(emit_raws, scope, items)
    return items


# -- indices ------------------------------------------------------------------------------

def _extern_func_index(files: list[RawFile], proj_id: str) -> dict[str, str]:
    """Every externally-linked function name -> its (program-global) Function id. Definitions
    and prototypes alike register the name; the id is the same either way."""
    idx: dict[str, str] = {}
    for rf in files:
        for fn in rf.functions:
            if not fn.is_static:
                idx.setdefault(fn.name, ids.function_id(proj_id, fn.name))
    return idx


def _type_index(files: list[RawFile], proj_id: str) -> dict[str, tuple[str, str]]:
    """Bare type name -> (label, id) for REFERENCES resolution. When a name is both a tag and
    a typedef (typedef struct Point {..} Point), the typedef wins for bare usage."""
    idx: dict[str, tuple[str, str]] = {}

    def offer(name: str, label: str, node_id: str) -> None:
        cur = idx.get(name)
        if cur is None or _TYPE_PRECEDENCE[label] > _TYPE_PRECEDENCE[cur[0]]:
            idx[name] = (label, node_id)

    for rf in files:
        for rec in rf.records:
            label = "Struct" if rec.kind == "struct" else "Union"
            offer(rec.name, label, _record_id(rec, proj_id))
        for en in rf.enums:
            offer(en.name, "Enum", ids.enum_id(proj_id, en.name))
        for td in rf.typedefs:
            offer(td.name, "Typedef", ids.typedef_id(proj_id, td.name))
    return idx


def _include_index(files: list[RawFile], file_ids: dict[str, str]) -> dict[str, str]:
    """Map normalized full paths AND basenames to File ids, so an #include resolves by exact
    project-relative path first, else by unique basename."""
    by_full: dict[str, str] = {}
    basename_count: dict[str, int] = {}
    for rf in files:
        norm = _norm(rf.path)
        by_full[norm] = file_ids[rf.path]
        basename_count[posixpath.basename(norm)] = basename_count.get(posixpath.basename(norm), 0) + 1
    idx = dict(by_full)
    for rf in files:
        base = posixpath.basename(_norm(rf.path))
        if basename_count[base] == 1:
            idx.setdefault(base, file_ids[rf.path])
    return idx


# -- emitters -----------------------------------------------------------------------------

def _emit_types(files: list[RawFile], proj_id: str, items: list, defined_elsewhere_types: frozenset = frozenset()) -> None:
    # Merge forward declarations with definitions: a definition (has body) wins. DECLARES for
    # a forward decl, DEFINES for the definition; one node per tag. `defined_elsewhere_types` (set
    # only on the incremental path) names types DEFINED in untouched files: a forward decl in the
    # emit set for one of these emits its DECLARES edge but no node (the real node is untouched).
    local_def_types = ({rec.name for rf in files for rec in rf.records if rec.is_definition}
                       | {en.name for rf in files for en in rf.enums if en.is_definition}
                       | {td.name for rf in files for td in rf.typedefs})
    records: dict[str, GraphNode] = {}
    record_defined: set[str] = set()
    for rf in files:
        fid = ids.file_id(proj_id, rf.path)
        for rec in rf.records:
            rid = _record_id(rec, proj_id)
            label = "Struct" if rec.kind == "struct" else "Union"
            if rec.is_definition and rid not in record_defined:
                record_defined.add(rid)
                records[rid] = GraphNode(label, {"id": rid}, {
                    "name": rec.name, "qualified_name": rec.name, "field_count": len(rec.fields),
                    "source": rec.source, "doc": rec.doc, **_loc(rf.path, rec.line_start, rec.line_end),
                })
                for pos, f in enumerate(rec.fields):
                    field_node_id = ids.field_id(rid, f.name)
                    items.append(GraphNode("Field", {"id": field_node_id}, {
                        "name": f.name, "qualified_name": f"{rec.name}.{f.name}", "type": f.type,
                        "position": pos, **_loc(rf.path, f.line_start, f.line_end),
                    }))
                    items.append(GraphEdge("HAS_FIELD", label, {"id": rid}, "Field", {"id": field_node_id}))
                items.append(GraphEdge("DEFINES", "File", {"id": fid}, label, {"id": rid}))
            elif not rec.is_definition and rec.name not in local_def_types and rec.name in defined_elsewhere_types:
                # Forward decl of a record DEFINED in an untouched file: DECLARES edge only, no node.
                items.append(GraphEdge("DECLARES", "File", {"id": fid}, label, {"id": rid}))
            else:
                records.setdefault(rid, GraphNode(label, {"id": rid}, {
                    "name": rec.name, "qualified_name": rec.name, **_loc(rf.path, rec.line_start, rec.line_end),
                }))
                edge = "DECLARES" if not rec.is_definition else "DEFINES"
                items.append(GraphEdge(edge, "File", {"id": fid}, label, {"id": rid}))
    items.extend(records.values())

    enums: dict[str, GraphNode] = {}
    for rf in files:
        fid = ids.file_id(proj_id, rf.path)
        for en in rf.enums:
            eid = ids.enum_id(proj_id, en.name)
            if en.is_definition:
                enums[eid] = GraphNode("Enum", {"id": eid}, {
                    "name": en.name, "qualified_name": en.name, "members": en.members,
                    "source": en.source, "doc": en.doc, **_loc(rf.path, en.line_start, en.line_end),
                })
                items.append(GraphEdge("DEFINES", "File", {"id": fid}, "Enum", {"id": eid}))
            elif en.name not in local_def_types and en.name in defined_elsewhere_types:
                pass  # forward decl of an enum DEFINED in an untouched file -> leave its node alone
            else:
                enums.setdefault(eid, GraphNode("Enum", {"id": eid}, {
                    "name": en.name, "qualified_name": en.name, **_loc(rf.path, en.line_start, en.line_end),
                }))
    items.extend(enums.values())

    typedefs: dict[str, GraphNode] = {}
    for rf in files:
        fid = ids.file_id(proj_id, rf.path)
        for td in rf.typedefs:
            tid = ids.typedef_id(proj_id, td.name)
            typedefs.setdefault(tid, GraphNode("Typedef", {"id": tid}, {
                "name": td.name, "qualified_name": td.name, "underlying": td.underlying,
                "source": td.source, "doc": td.doc, **_loc(rf.path, td.line_start, td.line_end),
            }))
            items.append(GraphEdge("DEFINES", "File", {"id": fid}, "Typedef", {"id": tid}))
    items.extend(typedefs.values())


def _emit_globals_and_macros(files: list[RawFile], proj_id: str, items: list) -> None:
    globals_seen: set[str] = set()
    macros_seen: set[str] = set()  # a name #defined twice in one file (#ifdef branches) collides on
    #                                one id -- keep the first (files sorted -> deterministic), else the
    #                                MERGE winner is arbitrary and even a full recompute isn't stable.
    for rf in files:
        fid = ids.file_id(proj_id, rf.path)
        norm = _norm(rf.path)
        for g in rf.globals:
            gid = ids.static_global_id(fid, g.name) if g.is_static else ids.global_id(proj_id, g.name)
            if gid not in globals_seen:
                globals_seen.add(gid)
                qn = f"{norm}:{g.name}" if g.is_static else g.name  # file-local statics disambiguate by file
                items.append(GraphNode("GlobalVariable", {"id": gid}, {
                    "name": g.name, "qualified_name": qn, "type": g.type,
                    "linkage": "internal" if g.is_static else "external",
                    "source": g.source, **_loc(rf.path, g.line_start, g.line_end),
                }))
            items.append(GraphEdge("DEFINES", "File", {"id": fid}, "GlobalVariable", {"id": gid}))
        for m in rf.macros:
            mid = ids.macro_id(fid, m.name)  # macros are file/preprocessor-scoped
            if mid in macros_seen:
                continue  # duplicate #define in the same file (guarded by #ifdef) -> keep the first
            macros_seen.add(mid)
            items.append(GraphNode("Macro", {"id": mid}, {
                "name": m.name, "qualified_name": f"{norm}:{m.name}", "function_like": m.is_function_like,
                "source": m.source, **_loc(rf.path, m.line_start, m.line_end),
            }))
            items.append(GraphEdge("DEFINES", "File", {"id": fid}, "Macro", {"id": mid}))


def _emit_functions(files: list[RawFile], scope: _Scope, items: list) -> None:
    # Merge prototype + definition into one node keyed by linkage-aware id.
    nodes: dict[str, GraphNode] = {}
    params_of: dict[str, tuple[str, RawFunction]] = {}  # fid -> (file_id, the definition's raw fn)
    defined: set[str] = set()
    # External functions DEFINED anywhere in THIS batch (so a prototype for one still needs no node).
    local_def_funcs = {fn.name for rf in files for fn in rf.functions if fn.is_definition and not fn.is_static}

    for rf in files:
        fid = scope.file_ids[rf.path]
        norm = _norm(rf.path)
        for fn in rf.functions:
            func_id = _func_id(fn, fid, scope.proj_id)
            props = {
                "name": fn.name,
                "qualified_name": f"{norm}:{fn.name}" if fn.is_static else fn.name,
                "return_type": fn.return_type,
                "signature": _signature(fn),
                "linkage": "internal" if fn.is_static else "external",
                "defined": fn.is_definition or func_id in defined,
                "source": fn.source,
                "doc": fn.doc,
                **_loc(rf.path, fn.line_start, fn.line_end),
            }
            if not fn.is_static:  # external linkage -> cross-repo key (ADR 0004); statics can't cross
                props["linkage_key"] = ids.extern_function_key(fn.name)
            if fn.is_definition:
                defined.add(func_id)
                nodes[func_id] = GraphNode("Function", {"id": func_id}, {**props, "defined": True})
                params_of[func_id] = (fid, fn)
                items.append(GraphEdge("DEFINES", "File", {"id": fid}, "Function", {"id": func_id}))
            elif (not fn.is_static and fn.name not in local_def_funcs
                    and fn.name in scope.defined_elsewhere_funcs):
                # Prototype for a function DEFINED in an untouched file: emit only its DECLARES edge.
                # The real node is owned by its definition and must not be overwritten by a sparse
                # re-emit (ADR 0005 definition-vs-declaration ownership).
                items.append(GraphEdge("DECLARES", "File", {"id": fid}, "Function", {"id": func_id}))
            else:
                nodes.setdefault(func_id, GraphNode("Function", {"id": func_id}, props))
                items.append(GraphEdge("DECLARES", "File", {"id": fid}, "Function", {"id": func_id}))
                params_of.setdefault(func_id, (fid, fn))

    # Mark declared-only nodes' `defined` correctly (a later-seen definition wins).
    for func_id, node in nodes.items():
        if func_id in defined and not node.properties.get("defined"):
            nodes[func_id] = GraphNode("Function", node.key, {**node.properties, "defined": True})
    items.extend(nodes.values())

    # Parameters (prefer the definition's, which has names) + HAS_PARAMETER.
    for func_id, (_fid, fn) in params_of.items():
        for p in fn.params:
            pname = p.name or f"arg{p.position}"
            pid = ids.parameter_id(func_id, p.position, pname)
            items.append(GraphNode("Parameter", {"id": pid}, {"name": pname, "type": p.type, "position": p.position}))
            items.append(GraphEdge("HAS_PARAMETER", "Function", {"id": func_id}, "Parameter", {"id": pid}))

    # CALLS (definitions only) + REFERENCES (return/param types, from any fn) -- name-based.
    # One CALLS edge per (caller, callee) pair: a caller that calls the same function at several
    # sites (common -- e.g. a `serverAssert` macro expanded on every branch) collapses to a single
    # edge, not one parallel edge per site. Parallel edges share the `(type, from, to)` key, which
    # the delta-sync reconciler keys on, so an unreconcilable multiplicity change (3 sites -> 2) would
    # otherwise strand a stale duplicate on resync; and a code graph only needs "A calls B", not the
    # count. `seen` keys on the resolved TARGET id so a resolved and an extern edge to the same node
    # also dedup. Spans the whole loop (not per-fn) so two files defining the same extern name -- the
    # same func_id -- can't re-emit its edges.
    seen_calls: set[tuple[str, str]] = set()
    for rf in files:
        fid = scope.file_ids[rf.path]
        for fn in rf.functions:
            func_id = _func_id(fn, fid, scope.proj_id)
            if fn.is_definition:
                for callee in fn.calls:
                    target = _resolve_call(callee, rf, fid, scope)
                    if target is not None:
                        if (func_id, target) not in seen_calls:
                            seen_calls.add((func_id, target))
                            items.append(GraphEdge("CALLS", "Function", {"id": func_id}, "Function", {"id": target}, {"resolution": "name"}))
                    else:
                        # Unresolved: a libc/system symbol (malloc/memcpy/fork) or a function from
                        # another repo not in this ingest -> a placeholder Function keyed by its
                        # cross-repo linkage, reached by an ordinary CALLS edge (ADR 0004).
                        eid = ids.extern_function_key(callee)
                        if (func_id, eid) in seen_calls:
                            continue
                        seen_calls.add((func_id, eid))
                        if eid not in scope.emitted_externs:
                            scope.emitted_externs.add(eid)
                            items.append(_external_function_node(callee))
                        items.append(GraphEdge("CALLS", "Function", {"id": func_id}, "Function", {"id": eid}, {"resolution": "external"}))
            for ref in (fn.return_type, *(p.type for p in fn.params)):
                hit = scope.type_index.get(_bare(ref))
                if hit is not None:
                    label, tid = hit
                    items.append(GraphEdge("REFERENCES", "Function", {"id": func_id}, label, {"id": tid}, {"resolution": "name"}))

    # REFERENCES from struct/union fields to named types.
    for rf in files:
        for rec in rf.records:
            if not rec.is_definition:
                continue
            rid = _record_id(rec, scope.proj_id)
            label = "Struct" if rec.kind == "struct" else "Union"
            for f in rec.fields:
                if f.type_ref and (hit := scope.type_index.get(f.type_ref)):
                    tlabel, tid = hit
                    if tid != rid:  # don't self-reference a recursive struct member
                        items.append(GraphEdge("REFERENCES", label, {"id": rid}, tlabel, {"id": tid}, {"resolution": "name"}))


def _emit_includes(files: list[RawFile], scope: _Scope, items: list) -> None:
    for rf in files:
        fid = scope.file_ids[rf.path]
        for inc in rf.includes:
            target = scope.include_index.get(_norm(inc.path)) or scope.include_index.get(posixpath.basename(inc.path))
            if target is not None and target != fid:
                items.append(GraphEdge("INCLUDES", "File", {"id": fid}, "File", {"id": target}, {"resolution": "name"}))


# -- helpers ------------------------------------------------------------------------------

def _norm(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def _loc(file_path: str, line_start: int, line_end: int) -> dict[str, object]:
    """Location properties shared with the other ingestors: the defining file and a 1-based
    inclusive line range (matching the Python ingestor's file_path / line_start / line_end)."""
    return {"file_path": _norm(file_path), "line_start": line_start, "line_end": line_end}


def _signature(fn: RawFunction) -> str:
    """A C function signature string, e.g. `int add(const int *a, int b)` -- the analog of the
    Python ingestor's `signature` property. Prefer the header text captured verbatim by the parser
    (keeps `const`/`*`/`[]` and the exact spelling); reconstruct from parts only if it's missing."""
    if fn.signature:
        return fn.signature
    params = ", ".join((p.type + (" " + p.name if p.name else "")).strip() for p in fn.params) or "void"
    return f"{fn.return_type} {fn.name}({params})".strip()


def _bare(type_text: str) -> str:
    """Strip struct/union/enum keyword, pointers and cv-qualifiers to the bare tag/type name."""
    t = type_text.replace("*", " ").replace("const", " ").replace("volatile", " ")
    parts = [p for p in t.split() if p not in ("struct", "union", "enum", "unsigned", "signed")]
    return parts[-1] if parts else ""


def _record_id(rec, proj_id: str) -> str:
    return ids.struct_id(proj_id, rec.name) if rec.kind == "struct" else ids.union_id(proj_id, rec.name)


def _func_id(fn: RawFunction, file_id: str, proj_id: str) -> str:
    return ids.static_function_id(file_id, fn.name) if fn.is_static else ids.function_id(proj_id, fn.name)


def _external_function_node(name: str) -> GraphNode:
    """A placeholder for an unresolved call, keyed by its cross-repo linkage so a later ingest of
    the defining repo takes it over (ADR 0004). Shallow: shape mirrors a real Function so the same
    CALLS queries reach it; `resolved:false` + `origin` mark it."""
    eid = ids.extern_function_key(name)
    return GraphNode("Function", {"id": eid}, {
        "name": name, "qualified_name": name, "resolved": False,
        "origin": _c_origin(name), "linkage_key": eid,
    })


def _resolve_call(callee: str, rf: RawFile, file_id: str, scope: _Scope) -> str | None:
    """A call resolves to a file-local static of that name first (internal linkage shadows),
    then to an external function; unresolved (a macro, or a libc/out-of-project symbol) -> no
    edge."""
    for fn in rf.functions:
        if fn.is_static and fn.name == callee:
            return ids.static_function_id(file_id, callee)
    return scope.extern_funcs.get(callee)
