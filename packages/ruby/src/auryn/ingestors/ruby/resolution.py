"""Project-wide pass (phase 2): turn the per-file RawFile list into graph nodes/edges, applying
the cross-file decisions a single file can't -- merging reopened namespaces (a class/module split
across files is one node), materializing the class/module tier, attaching methods/fields/consts to
their owning namespace, resolving the superclass (EXTENDS) and mixins (INCLUDES) to project
namespaces, the `require` graph (IMPORTS) to project files, and best-effort call resolution.

Name-based only -- this is the fast tree-sitter engine, so every resolved CALLS/EXTENDS/INCLUDES
edge is stamped `resolution="name"`. Ruby is DYNAMIC (open classes, method_missing, no static
receiver types), so this pass is deliberately conservative:

- Receiverless (`foo`) and `self.foo` calls resolve against the enclosing class/module's own
  methods and its project ancestors (superclass chain + included modules), then top-level
  functions. `Const.foo` / `A::B.foo` resolve to a class/singleton method on that project
  namespace (`Foo.new` resolves to `Foo#initialize`). `super` resolves the same-named method up
  the ancestor chain.
- A `Const.foo` whose constant is NOT a project namespace is a nameable external symbol
  (`JSON.parse`, `Net::HTTP.get`) -> an ADR-0004 placeholder Function reached by an ordinary CALLS
  edge, healed if the defining repo is later ingested.
- Calls on a *local/@ivar* receiver (`x.foo`, `@store.foo`) and complex chains (`a.b.foo`) are too
  dynamic to name without type inference -> dropped (no wrong guess), left for a deep pass.

Unresolved intra-project references produce no edge rather than a wrong guess -- the same
trade-off as the Rust/Go/Java ingestors."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field

from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.ingestors.ruby import ids
from auryn.ingestors.ruby.raw_model import RawFile, RawMethod, RawModule

# ADR 0004: constant-receiver calls into these well-known Ruby core/stdlib namespaces surface with
# origin="stdlib" (a terminal external that never resolves to an ingested repo); any other external
# constant is origin="external" (a candidate a sibling repo could later define). Not exhaustive --
# covers the common surface. Matched on the FIRST `::` segment (`Net::HTTP` -> `Net`).
_STDLIB_ROOTS = frozenset({
    "Kernel", "Object", "BasicObject", "Module", "Class", "Comparable", "Enumerable",
    "String", "Symbol", "Integer", "Float", "Numeric", "Array", "Hash", "Set", "Range",
    "Struct", "Time", "Date", "DateTime", "File", "Dir", "IO", "Math", "Random",
    "Regexp", "MatchData", "Proc", "Method", "Thread", "Mutex", "Fiber", "Marshal",
    "JSON", "YAML", "Base64", "Digest", "SecureRandom", "OpenSSL", "Net", "URI", "Socket",
    "Logger", "ObjectSpace", "GC", "Process", "Signal", "ENV", "ARGV", "StringIO", "Tempfile",
})


@dataclass
class _NS:
    """A merged namespace accumulator (reopened class/module -> one entry keyed by ns_id)."""
    ns_id: str
    label: str  # "Class" | "Module"
    qualified: str
    simple: str
    file_id: str
    first: RawModule
    parent_qualified: str
    superclasses: list[str] = field(default_factory=list)
    includes: list = field(default_factory=list)  # RawInclude
    methods: list = field(default_factory=list)  # (RawMethod,)
    fields: list = field(default_factory=list)  # RawField
    consts: list = field(default_factory=list)  # RawConst


@dataclass
class _Callable:
    id: str
    label: str  # "Function" | "Method"
    scope_id: str  # owning ns_id (Method) or project_id (Function)
    owner: _NS | None
    raw: RawMethod


def resolve(raw_files, project_id: str) -> Iterator[GraphNode | GraphEdge]:
    # -- files -----------------------------------------------------------------------------
    file_ids: dict[str, str] = {}
    norm_index: dict[str, str] = {}  # normalized rel path -> file_id (for require resolution)
    for rf in raw_files:
        fid = ids.file_id(project_id, rf.path)
        file_ids[rf.path] = fid
        norm_index[_norm(rf.path)] = fid
        yield GraphNode("File", {"id": fid}, {"name": _basename(rf.path), "path": _norm(rf.path)})
        yield GraphEdge("HAS_FILE", "Project", {"id": project_id}, "File", {"id": fid})

    # -- collect + merge namespaces --------------------------------------------------------
    registry: dict[str, _NS] = {}
    order: list[str] = []  # ns_ids in first-seen order (deterministic emission)
    for rf in raw_files:
        _collect(rf, project_id, "", file_ids[rf.path], registry, order)

    by_qualified: dict[str, _NS] = {}
    by_simple: dict[str, list[_NS]] = defaultdict(list)
    for ns_id in order:
        ns = registry[ns_id]
        by_qualified.setdefault(ns.qualified, ns)
        by_simple[ns.simple].append(ns)

    # -- callables + member indexes --------------------------------------------------------
    callables: list[_Callable] = []
    function_index: dict[str, list[str]] = defaultdict(list)  # name -> [func_id]
    # ns_id -> name -> [(method_id, is_singleton)]
    method_map: dict[str, dict[str, list[tuple[str, bool]]]] = defaultdict(lambda: defaultdict(list))

    for rf in raw_files:  # top-level defs -> Functions on the Project
        for m in rf.methods:
            if m.kind != "FUNCTION":
                continue
            fid = ids.function_id(project_id, m.name, [p.name for p in m.parameters])
            callables.append(_Callable(fid, "Function", project_id, None, m))
            function_index[m.name].append(fid)

    for ns_id in order:
        ns = registry[ns_id]
        for m in ns.methods:
            mid = ids.method_id(ns_id, m.name, [p.name for p in m.parameters], m.is_singleton)
            callables.append(_Callable(mid, "Method", ns_id, ns, m))
            method_map[ns_id][m.name].append((mid, m.is_singleton))

    ancestors = _build_ancestors(order, registry, by_qualified, by_simple)

    # -- pass 1: nodes + structural edges (landing nodes for pass 2) -----------------------
    for ns_id in order:
        yield from _emit_namespace(registry[ns_id], project_id, by_qualified)
    for cal in callables:
        yield from _emit_callable(cal, project_id)

    # -- pass 2: EXTENDS, INCLUDES, IMPORTS, CALLS -----------------------------------------
    for ns_id in order:
        ns = registry[ns_id]
        if ns.label == "Class":
            for sc in ns.superclasses:
                target = _resolve_ns(sc, by_qualified, by_simple)
                if target is not None and target.label == "Class" and target.ns_id != ns.ns_id:
                    yield GraphEdge("EXTENDS", "Class", {"id": ns.ns_id}, "Class", {"id": target.ns_id},
                                    {"resolution": "name"})
                    break  # a Ruby class has a single superclass
        emitted_inc: set[str] = set()
        for inc in ns.includes:
            target = _resolve_ns(inc.name, by_qualified, by_simple)
            if target is None or target.label != "Module" or target.ns_id in emitted_inc:
                continue
            emitted_inc.add(target.ns_id)
            yield GraphEdge("INCLUDES", ns.label, {"id": ns.ns_id}, "Module", {"id": target.ns_id},
                            {"kind": inc.kind, "resolution": "name"})

    for rf in raw_files:
        yield from _emit_imports(rf, file_ids[rf.path], norm_index)

    emitted_externs: set[str] = set()
    for cal in callables:
        yield from _emit_calls(cal, method_map, ancestors, function_index, by_qualified, by_simple,
                               emitted_externs)


# -- collection ----------------------------------------------------------------------------

def _collect(scope, project_id: str, parent_qualified: str, file_id: str,
             registry: dict[str, _NS], order: list[str]) -> None:
    """Walk a RawFile / RawModule, merging each class/module into `registry` by its fully-qualified
    id. `parent_qualified` is the lexical enclosing namespace's qualified name ("" at file top)."""
    for rm in getattr(scope, "classes", []):
        _merge_ns(rm, "Class", project_id, parent_qualified, file_id, registry, order)
    for rm in getattr(scope, "modules", []):
        _merge_ns(rm, "Module", project_id, parent_qualified, file_id, registry, order)


def _merge_ns(rm: RawModule, label: str, project_id: str, parent_qualified: str, file_id: str,
              registry: dict[str, _NS], order: list[str]) -> None:
    qualified = f"{parent_qualified}::{rm.name}" if parent_qualified else rm.name
    ns_id = ids.class_id(project_id, qualified) if label == "Class" else ids.module_id(project_id, qualified)
    ns = registry.get(ns_id)
    if ns is None:
        ns = _NS(
            ns_id=ns_id, label=label, qualified=qualified, simple=qualified.split("::")[-1],
            file_id=file_id, first=rm, parent_qualified=parent_qualified,
        )
        registry[ns_id] = ns
        order.append(ns_id)
    if rm.superclass:
        ns.superclasses.append(rm.superclass)
    ns.includes.extend(rm.includes)
    ns.methods.extend(rm.methods)
    ns.fields.extend(rm.fields)
    ns.consts.extend(rm.consts)
    _collect(rm, project_id, qualified, file_id, registry, order)


def _build_ancestors(order, registry, by_qualified, by_simple) -> dict[str, list[str]]:
    """Per namespace: the project ancestor ns_ids (superclass chain first, then included modules),
    for method-dispatch resolution. Cycle-safe and bounded."""
    out: dict[str, list[str]] = {}
    for ns_id in order:
        ns = registry[ns_id]
        chain: list[str] = []
        seen = {ns_id}
        # superclass chain
        cur = ns
        while cur is not None:
            nxt = None
            for sc in cur.superclasses:
                cand = _resolve_ns(sc, by_qualified, by_simple)
                if cand is not None and cand.label == "Class" and cand.ns_id not in seen:
                    nxt = cand
                    break
            if nxt is None:
                break
            chain.append(nxt.ns_id)
            seen.add(nxt.ns_id)
            cur = nxt
        # included modules of this ns and of every class in the chain
        for base_id in [ns_id, *chain]:
            for inc in registry[base_id].includes:
                cand = _resolve_ns(inc.name, by_qualified, by_simple)
                if cand is not None and cand.label == "Module" and cand.ns_id not in seen:
                    chain.append(cand.ns_id)
                    seen.add(cand.ns_id)
        out[ns_id] = chain
    return out


# -- pass 1 emitters -----------------------------------------------------------------------

def _emit_namespace(ns: _NS, project_id: str, by_qualified) -> Iterator[GraphNode | GraphEdge]:
    rm = ns.first
    props = {
        "name": ns.simple,
        "qualified_name": ns.qualified,
        "source": rm.source,
        "doc": rm.doc,
        "line_start": rm.line_start,
        "line_end": rm.line_end,
    }
    if ns.label == "Class":
        props["superclass"] = ns.superclasses[0] if ns.superclasses else None
    yield GraphNode(ns.label, {"id": ns.ns_id}, props)

    parent = by_qualified.get(ns.parent_qualified) if ns.parent_qualified else None
    if parent is not None:
        rel = "HAS_TYPE" if ns.label == "Class" else "HAS_MODULE"
        yield GraphEdge(rel, parent.label, {"id": parent.ns_id}, ns.label, {"id": ns.ns_id})
    else:
        rel = "HAS_TYPE" if ns.label == "Class" else "HAS_MODULE"
        yield GraphEdge(rel, "Project", {"id": project_id}, ns.label, {"id": ns.ns_id})
    yield GraphEdge("DEFINED_IN", ns.label, {"id": ns.ns_id}, "File", {"id": ns.file_id})

    emitted_fields: set[str] = set()
    for f in ns.fields:
        fid = ids.field_id(ns.ns_id, f.name)
        if fid in emitted_fields:
            continue
        emitted_fields.add(fid)
        yield GraphNode("Field", {"id": fid}, {
            "name": f.name, "access": f.access, "line_start": f.line_start, "line_end": f.line_end,
        })
        yield GraphEdge("HAS_FIELD", ns.label, {"id": ns.ns_id}, "Field", {"id": fid})

    emitted_consts: set[str] = set()
    for c in ns.consts:
        cid = ids.const_id(ns.ns_id, c.name)
        if cid in emitted_consts:
            continue
        emitted_consts.add(cid)
        yield GraphNode("Const", {"id": cid}, {
            "name": c.name, "value": c.value, "line_start": c.line_start, "line_end": c.line_end,
        })
        yield GraphEdge("HAS_CONST", ns.label, {"id": ns.ns_id}, "Const", {"id": cid})


def _emit_callable(cal: _Callable, project_id: str) -> Iterator[GraphNode | GraphEdge]:
    m = cal.raw
    props: dict = {
        "name": m.name,
        "kind": m.kind,
        "visibility": m.visibility,
        "signature": m.signature,
        "source": m.source,
        "doc": m.doc,
        "line_start": m.line_start,
        "line_end": m.line_end,
    }
    linkage = _linkage(cal)
    if linkage is not None:
        props["linkage_key"] = linkage
    if cal.label == "Method":
        props["is_singleton"] = m.is_singleton
        yield GraphNode("Method", {"id": cal.id}, props)
        yield GraphEdge("HAS_METHOD", cal.owner.label, {"id": cal.owner.ns_id}, "Method", {"id": cal.id})
    else:
        yield GraphNode("Function", {"id": cal.id}, props)
        yield GraphEdge("HAS_FUNCTION", "Project", {"id": project_id}, "Function", {"id": cal.id})
    for i, p in enumerate(m.parameters):
        pid = ids.parameter_id(cal.id, i, p.name)
        yield GraphNode("Parameter", {"id": pid}, {
            "name": p.name, "kind": p.kind, "position": p.position, "default": p.default,
        })
        yield GraphEdge("HAS_PARAMETER", cal.label, {"id": cal.id}, "Parameter", {"id": pid})


def _linkage(cal: _Callable) -> str | None:
    """ADR 0004 cross-repo key for a public callable (Ruby defaults to public), else None. In the
    form a consumer writes: `Class#method` (instance), `Class.method` (class/singleton), or a bare
    `name` (top-level function)."""
    m = cal.raw
    if m.visibility != "public":
        return None
    if cal.label == "Method":
        sep = "." if m.is_singleton else "#"
        return ids.extern_symbol_key(f"{cal.owner.qualified}{sep}{m.name}")
    return ids.extern_symbol_key(m.name)


# -- pass 2: imports + calls ---------------------------------------------------------------

def _emit_imports(rf: RawFile, from_file_id: str, norm_index: dict[str, str]) -> Iterator[GraphEdge]:
    emitted: set[str] = set()
    for req in rf.requires:
        target_id = _resolve_require(req, rf.path, norm_index)
        if target_id is None or target_id == from_file_id or target_id in emitted:
            continue
        emitted.add(target_id)
        resolution = "path" if req.kind == "require_relative" else "name"
        yield GraphEdge("IMPORTS", "File", {"id": from_file_id}, "File", {"id": target_id},
                        {"resolution": resolution, "kind": req.kind, "target": req.target})


def _resolve_require(req, from_path: str, norm_index: dict[str, str]) -> str | None:
    target = req.target
    if not target:
        return None
    candidate = target if target.endswith(".rb") else target + ".rb"
    if req.kind == "require_relative":
        base = _norm(from_path).rsplit("/", 1)[0] if "/" in _norm(from_path) else ""
        joined = _norm(f"{base}/{candidate}" if base else candidate)
        joined = _collapse(joined)
        return norm_index.get(joined)
    # `require "x/y"`: $LOAD_PATH-relative -> match a project file by exact or suffix path.
    cand = _norm(candidate)
    if cand in norm_index:
        return norm_index[cand]
    suffix = "/" + cand
    matches = sorted(fid_path for fid_path in norm_index if fid_path.endswith(suffix))
    return norm_index[matches[0]] if matches else None


def _emit_calls(cal: _Callable, method_map, ancestors, function_index, by_qualified, by_simple,
                emitted_externs: set[str]) -> Iterator[GraphNode | GraphEdge]:
    emitted: set[str] = set()
    for call in cal.raw.calls:
        hit = _resolve_call(call, cal, method_map, ancestors, function_index, by_qualified, by_simple)
        if hit is None:
            continue
        if isinstance(hit, _External):
            if hit.eid in emitted:
                continue
            emitted.add(hit.eid)
            if hit.eid not in emitted_externs:
                emitted_externs.add(hit.eid)
                yield GraphNode("Function", {"id": hit.eid}, {
                    "name": hit.name, "qualified_name": hit.qualified, "resolved": False,
                    "origin": hit.origin, "linkage_key": hit.eid,
                })
            yield GraphEdge("CALLS", cal.label, {"id": cal.id}, "Function", {"id": hit.eid},
                            {"resolution": "external", "receiver": hit.qualifier})
            continue
        target_id, extra = hit
        if target_id == cal.id or target_id in emitted:
            continue
        emitted.add(target_id)
        target_label = "Function" if ".func." in target_id else "Method"
        yield GraphEdge("CALLS", cal.label, {"id": cal.id}, target_label, {"id": target_id},
                        {"resolution": "name", **extra})


@dataclass
class _External:
    eid: str
    name: str
    qualified: str
    origin: str
    qualifier: str


def _resolve_call(call, cal: _Callable, method_map, ancestors, function_index, by_qualified, by_simple):
    """A resolved local `(target_id, extra_props)`, an `_External` placeholder for a nameable
    out-of-project constant-receiver call (ADR 0004), or None to drop."""
    owner_id = cal.owner.ns_id if cal.owner is not None else None

    if call.kind in ("command", "self"):
        if owner_id is not None:
            mid = _instance_method(owner_id, call.name, method_map, ancestors)
            if mid:
                return mid, {}
        if call.kind == "command":  # a top-level function is only reachable receiverless
            fns = function_index.get(call.name)
            if fns:
                return sorted(fns)[0], {}
        return None

    if call.kind == "super":
        if owner_id is None:
            return None
        for aid in ancestors.get(owner_id, []):
            for (mid, is_singleton) in method_map.get(aid, {}).get(cal.raw.name, []):
                if not is_singleton:
                    return mid, {}
        return None

    if call.kind == "constant":
        ns = _resolve_ns(call.qualifier, by_qualified, by_simple)
        if ns is not None:
            if call.name == "new" and ns.label == "Class":  # Foo.new -> Foo#initialize
                mid = _instance_method(ns.ns_id, "initialize", method_map, ancestors)
                if mid:
                    return mid, {"receiver": ns.qualified}
            mid = _class_method(ns.ns_id, call.name, method_map, ancestors)
            if mid:
                return mid, {"receiver": ns.qualified}
            return None  # project namespace but no such class method -> intra-project dangling
        # a constant that names no project namespace -> external (stdlib/gem)
        qualified = f"{call.qualifier}.{call.name}"
        root = call.qualifier.split("::", 1)[0]
        origin = "stdlib" if root in _STDLIB_ROOTS else "external"
        return _External(ids.extern_symbol_key(qualified), call.name, qualified, origin, call.qualifier)

    # "instance" (local/@ivar receiver) and "chain" -> too dynamic to name -> drop.
    return None


def _instance_method(owner_id, name, method_map, ancestors) -> str | None:
    for aid in [owner_id, *ancestors.get(owner_id, [])]:
        for (mid, is_singleton) in method_map.get(aid, {}).get(name, []):
            if not is_singleton:
                return mid
    return None


def _class_method(owner_id, name, method_map, ancestors) -> str | None:
    for aid in [owner_id, *ancestors.get(owner_id, [])]:
        for (mid, is_singleton) in method_map.get(aid, {}).get(name, []):
            if is_singleton:
                return mid
    return None


# -- helpers -------------------------------------------------------------------------------

def _resolve_ns(name: str | None, by_qualified, by_simple) -> _NS | None:
    """Resolve a written constant name to a project namespace: try the fully-qualified path first,
    then the bare simple name (last segment), deterministically on ambiguity."""
    if not name:
        return None
    key = name.replace("::", "::").strip()
    if key in by_qualified:
        return by_qualified[key]
    # strip a leading `::` absolute qualifier, retry
    if key.startswith("::") and key[2:] in by_qualified:
        return by_qualified[key[2:]]
    simple = key.split("::")[-1]
    matches = by_simple.get(simple)
    if matches:
        return sorted(matches, key=lambda n: n.ns_id)[0]
    return None


def _norm(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def _collapse(path: str) -> str:
    """Collapse `.`/`..` segments in a normalized relative path (for require_relative)."""
    parts: list[str] = []
    for seg in path.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
        else:
            parts.append(seg)
    return "/".join(parts)


def _basename(path: str) -> str:
    return _norm(path).rsplit("/", 1)[-1]
