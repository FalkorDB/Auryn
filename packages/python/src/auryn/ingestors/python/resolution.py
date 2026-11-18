"""Phase 2 of the file-based Python engines: turn per-file raw models into the graph.

Runs once, project-wide (imports/inheritance/calls are cross-file), and is parameterized
by an optional `SemanticOracle`:

  * oracle=None -> the **ast engine** (zero extra deps): types are `annotated` or
    `unresolved` (never `inferred`); calls resolve only when statically import-bound;
    dynamic dispatch is not attempted (`unresolved_calls` stays empty).
  * oracle=<JediOracle> -> the **jedi engine** (default): unannotated types are *inferred*
    at their source position, and calls resolve by inference -- including receiver-typed
    calls like `conn.execute()` that the ast engine can't reach.

Both share the id scheme (ids.py) and the same node/edge model, so their output is
interchangeable for the backbone; they differ only in how much they resolve, which the
`type_source`/`resolution` provenance records honestly.
"""

from __future__ import annotations

import builtins
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Protocol

from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.ingestors.python import ids
from auryn.ingestors.python.raw_model import RawAttribute, RawCall, RawClass, RawFunction, RawModule

_PROTOCOL_BASES = {"Protocol", "typing.Protocol"}
_ABC_BASES = {"ABC", "abc.ABC"}

# ADR 0004: classify an unresolved reference so a never-resolves system symbol (malloc/len/
# os.fork) is distinguishable from a not-yet-ingested sibling repo. `builtins`/stdlib are precise;
# everything else is `external` -- the candidate a future `ingest` upgrades in place.
_STDLIB = getattr(sys, "stdlib_module_names", frozenset())
_BUILTINS = frozenset(dir(builtins))


def _origin(qualifier: str) -> str:
    top = qualifier.split(".", 1)[0] if qualifier else ""
    if top == "builtins":
        return "builtin"
    if top in _STDLIB:
        return "stdlib"
    return "external"


class SemanticOracle(Protocol):
    def infer_type(self, module_path: str, line: int, col: int) -> str | None:
        """Inferred type name at a position, or None when it can't be determined."""

    def resolve_call(self, module_path: str, line: int, col: int) -> str | None:
        """The callee's dotted target (e.g. "app.db.Connection.execute") at a position, or
        None when the oracle finds nothing. Whether the call is receiver-typed and what the
        receiver type is are derived by the caller from the call shape + this name."""


@dataclass(frozen=True)
class _Call:
    id: str
    label: str
    resolution: str
    receiver_type: str | None


@dataclass(frozen=True)
class _External:
    """A reference the resolver could name but that is not defined in this ingest: a stdlib/
    builtin/third-party symbol, or a sibling repo's symbol not yet ingested. Becomes a
    placeholder node keyed by its linkage (ADR 0004)."""

    qualifier: str  # dotted module (top-level symbol) or class qname (method)
    name: str
    label: str = "Function"


@dataclass
class _ModCtx:
    idx: "_Index"
    import_map: dict
    local_funcs: set
    module_path: str
    module_dotted: str
    oracle: SemanticOracle | None


class _Index:
    """Project-wide lookups built before emission."""

    def __init__(self, proj_id: str, modules: list[RawModule] = ()):
        self.proj_id = proj_id
        self.packages: set[str] = set()
        # Every importable project module/package dotted path -- so an import can be told
        # apart from an external one (only project targets get an IMPORTS edge).
        self.module_dotteds: set[str] = set()
        self.class_by_qname: dict[str, str] = {}
        self.callable_by_qname: dict[str, tuple[str, str]] = {}  # qname -> (id, "Function"|"Method")
        self.parents: dict[str, list[str]] = {}
        self.emitted_externs: set[str] = set()  # placeholder ids already emitted this pass (dedup)
        for m in modules:
            self.add_module(m)

    def add_module(self, m: RawModule) -> None:
        """Index one module's symbol contributions. Factored out of __init__ so build_plan
        (streaming) can feed modules one at a time as it loads them, dropping each after."""
        if m.dotted:
            self.module_dotteds.add(m.dotted)
            if m.is_package:
                self.packages.add(m.dotted)
        scope_id = _scope_id(self.proj_id, m)
        for fn in m.functions:
            self._index_function(fn, m.dotted, scope_id)
        for cls in m.classes:
            self._index_class(cls, m.dotted, scope_id)

    def _index_function(self, fn: RawFunction, parent_dotted: str, parent_scope_id: str):
        """Index a function and (recursively) the functions nested in its body. A nested def is
        scoped under its enclosing function's id/qname (`<module>.outer.inner`)."""
        qname = f"{parent_dotted}.{fn.name}"
        fid = ids.function_id(parent_scope_id, fn.name, [p.name for p in fn.params])
        self.callable_by_qname[qname] = (fid, "Function")
        for nested in fn.nested:
            self._index_function(nested, qname, fid)

    def _index_class(self, cls: RawClass, scope_dotted: str, scope_id: str):
        qname = f"{scope_dotted}.{cls.name}"
        cls_id = ids.class_id(scope_id, cls.name)
        self.class_by_qname[qname] = cls_id
        for method in cls.methods:
            mid = ids.method_id(cls_id, method.name, [p.name for p in method.params])
            self.callable_by_qname[f"{qname}.{method.name}"] = (mid, "Method")
            for nested in method.nested:  # a def inside a method is a nested Function scoped under it
                self._index_function(nested, f"{qname}.{method.name}", mid)
        for nested in cls.nested:
            self._index_class(nested, qname, cls_id)

    def container_package(self, dotted: str) -> str | None:
        parts = dotted.split(".")
        for cut in range(len(parts) - 1, 0, -1):
            prefix = ".".join(parts[:cut])
            if prefix in self.packages:
                return prefix
        return None


def _scope_id(proj_id: str, m: RawModule) -> str:
    """The id that owns a module's top-level symbols: the Package node for an __init__.py
    (so its classes/functions are emitted and reachable, not dropped), else the Module node."""
    return ids.package_id(proj_id, m.dotted) if m.is_package else ids.module_id(proj_id, m.dotted)


def resolve(modules: list[RawModule], proj_id: str, oracle: SemanticOracle | None = None) -> Iterator[GraphNode | GraphEdge]:
    idx = _Index(proj_id, modules)
    yield from _emit_packages(idx)
    for m in modules:
        yield from _emit_module(m, idx, oracle)


@dataclass
class GraphIndex:
    """The subset of `_Index` that resolution needs from *other* modules to resolve one
    module's cross-file references -- and that is exactly reproducible from the graph
    (qnames + ids are stored precisely; ids never embed volatile inferred types). Used by
    `resolve_incremental` to seed the symbol table for modules it is NOT re-emitting, so an
    incremental resync can parse+emit only the changed/affected modules while still binding
    their calls/bases/returns to untouched project symbols. See ADR 0002."""

    packages: set[str] = field(default_factory=set)
    module_dotteds: set[str] = field(default_factory=set)
    class_by_qname: dict[str, str] = field(default_factory=dict)
    callable_by_qname: dict[str, tuple[str, str]] = field(default_factory=dict)
    parents: dict[str, list[str]] = field(default_factory=dict)


def resolve_incremental(
    emit_modules: list[RawModule], proj_id: str, seed: GraphIndex, oracle: SemanticOracle | None = None
) -> Iterator[GraphNode | GraphEdge]:
    """Resolve+emit ONLY `emit_modules` (the changed+affected set), binding their references
    against a symbol table = these modules' own symbols merged over `seed` (every OTHER
    module's symbols, read back from the graph). The output is the target subgraph for
    exactly the emit set -- a scoped read-back + compute_delta then reconciles it. See
    incremental.py for the orchestration and the correctness argument (ADR 0002).

    NOTE: `seed.parents` is fully populated up front, whereas `resolve()` builds `parents`
    incrementally in emission order; for cross-module inheritance whose base is defined
    "later", the MRO linearization can differ. That divergence is exactly what the
    equivalence oracle checks for; deep inheritance is out of scope for this fast-engine
    prototype."""
    idx = _Index(proj_id, emit_modules)
    idx.packages |= seed.packages
    idx.module_dotteds |= seed.module_dotteds
    for qname, cid in seed.class_by_qname.items():
        idx.class_by_qname.setdefault(qname, cid)
    for qname, target in seed.callable_by_qname.items():
        idx.callable_by_qname.setdefault(qname, target)
    for qname, bases in seed.parents.items():
        idx.parents.setdefault(qname, bases)

    for dotted in sorted({m.dotted for m in emit_modules if m.is_package and m.dotted}):
        yield from _emit_one_package(idx, dotted)
    for m in emit_modules:
        yield from _emit_module(m, idx, oracle)


# -- streaming resolution (memory-bounded: source re-parsed per module, none held) ----------
#
# resolve() holds every RawModule (all def/class source text) in memory at once; for a large
# first ingest that dominates peak RSS. build_plan() instead scans each module once to build
# only the lightweight _Index (qname -> id tables, no source), and emit_streaming() emits the
# SAME nodes/edges but loads one module at a time. Because Python resolves relative imports at
# PARSE time (from the module's dotted path), a blob-sha pickle cache would be stale on a move;
# ast.parse is cheap, so load_fn just re-parses the blob -- always with the current layout.
# `idx.parents` is (re)built during emission in module order, NOT up front, so the MRO matches
# resolve() exactly (up-front parents is resolve_incremental's behaviour -- see its note).


@dataclass
class StreamPlan:
    idx: _Index
    module_refs: list  # [(path, blob_sha)]
    broken: list       # paths that failed to parse (parse_ok False)


def build_plan(module_refs, load_fn, proj_id: str) -> StreamPlan:
    """Phase 1: scan each module once into the lightweight _Index, dropping it after.
    `module_refs` is (path, blob_sha) pairs; `load_fn(path, blob_sha)` returns a fresh
    RawModule (a re-parse of the blob at the current path/layout)."""
    module_refs = list(module_refs)
    idx = _Index(proj_id)
    broken: list[str] = []
    for path, blob_sha in module_refs:
        m = load_fn(path, blob_sha)
        if not m.parse_ok:
            broken.append(m.path)
        idx.add_module(m)
        del m
    return StreamPlan(idx=idx, module_refs=module_refs, broken=sorted(broken))


def emit_streaming(plan: StreamPlan, load_fn, proj_id: str, oracle: SemanticOracle | None = None) -> Iterator[GraphNode | GraphEdge]:
    """Phase 2: yield the same nodes/edges as resolve(), re-loading one module at a time.
    Reuses _emit_packages/_emit_module unchanged. Safe to iterate repeatedly (each call
    reloads and resets the per-pass state) -- stream_apply iterates it twice. `idx.parents`
    and `idx.emitted_externs` are reset per call so each pass rebuilds them in module order,
    matching resolve()'s single-pass behaviour; a cross-module edge is still written only once
    every node exists, because stream_apply writes all nodes before any edge."""
    idx = plan.idx
    idx.parents = {}
    idx.emitted_externs = set()
    yield from _emit_packages(idx)
    for path, blob_sha in plan.module_refs:
        m = load_fn(path, blob_sha)
        yield from _emit_module(m, idx, oracle)
        del m


# -- packages ----------------------------------------------------------------------


def _emit_packages(idx: _Index) -> Iterator[GraphNode | GraphEdge]:
    for dotted in sorted(idx.packages):
        yield from _emit_one_package(idx, dotted)


def _emit_one_package(idx: _Index, dotted: str) -> Iterator[GraphNode | GraphEdge]:
    pkg_id = ids.package_id(idx.proj_id, dotted)
    yield GraphNode("Package", {"id": pkg_id}, {"name": dotted.rsplit(".", 1)[-1], "dotted_path": dotted,
                                                "linkage_key": ids.extern_module_key(dotted)})
    parent = idx.container_package(dotted)
    if parent:
        yield GraphEdge("HAS_SUBPACKAGE", "Package", {"id": ids.package_id(idx.proj_id, parent)}, "Package", {"id": pkg_id})
    else:
        yield GraphEdge("HAS_PACKAGE", "Project", {"id": idx.proj_id}, "Package", {"id": pkg_id})


# -- modules -----------------------------------------------------------------------


def _emit_module(m: RawModule, idx: _Index, oracle: SemanticOracle | None) -> Iterator[GraphNode | GraphEdge]:
    scope_id = _scope_id(idx.proj_id, m)
    if m.is_package:
        # The Package node + its hierarchy edge are emitted by _emit_packages(); an
        # __init__.py's own top-level symbols hang off that Package node.
        scope_label = "Package"
    else:
        scope_label = "Module"
        yield GraphNode(
            "Module",
            {"id": scope_id},
            {"name": m.dotted.rsplit(".", 1)[-1], "dotted_path": m.dotted, "file_path": m.path,
             "linkage_key": ids.extern_module_key(m.dotted)},
        )
        parent = idx.container_package(m.dotted)
        if parent:
            yield GraphEdge("HAS_MODULE", "Package", {"id": ids.package_id(idx.proj_id, parent)}, "Module", {"id": scope_id})
        else:
            yield GraphEdge("HAS_MODULE", "Project", {"id": idx.proj_id}, "Module", {"id": scope_id})

    ctx = _ModCtx(
        idx=idx,
        import_map={imp.local: imp for imp in m.imports},
        local_funcs={fn.name for fn in m.functions},
        module_path=m.path,
        module_dotted=m.dotted,
        oracle=oracle,
    )
    for fn in m.functions:
        yield from _emit_function(fn, scope_id, m.dotted, m.dotted, "HAS_FUNCTION", scope_id, scope_label, ctx)
    for cls in m.classes:
        yield from _emit_class(cls, scope_id, m.dotted, scope_id, scope_label, "HAS_CLASS", ctx)
    yield from _emit_imports(m, scope_id, scope_label, idx)

    # Module-scope (top-level) calls: the Module (or Package, for an __init__.py) node is the
    # CALLS source for a script's driver code -- the only place a non-callable node originates a
    # CALLS edge. Resolved exactly like a function body (same helper), so a top-level `main()`
    # binds to the project function and a top-level `print()` becomes a builtin placeholder.
    mod_call_edges, mod_externals, _ = _resolve_scope_calls(m.calls, scope_label, scope_id, ctx, m.dotted)
    yield from mod_externals
    yield from mod_call_edges


def _emit_imports(m: RawModule, scope_id: str, scope_label: str, idx: _Index) -> Iterator[GraphNode | GraphEdge]:
    """One IMPORTS edge per module this module imports from, aggregating the bound names (a bare
    `import a.b` targets module a.b; `from a.b import C` targets a.b). A project target edges to
    the real Module/Package (`resolution:"resolved"`); an external target (stdlib/third-party or a
    not-yet-ingested sibling repo) edges to a placeholder Module node keyed by linkage
    (`resolution:"external"`, ADR 0004) so the provenance "X came from module B" is kept and can
    later upgrade. A self-import is skipped. Aggregated + name-sorted for determinism."""
    project: dict[str, set[str]] = {}
    external: dict[str, set[str]] = {}
    for imp in m.imports:
        target_module = imp.target if imp.is_module else imp.target.rsplit(".", 1)[0]
        if not target_module or target_module == m.dotted:
            continue
        bucket = project if target_module in idx.module_dotteds else external
        bucket.setdefault(target_module, set()).add(imp.local)
    for target_module in sorted(project):
        if target_module in idx.packages:
            to_label, to_id = "Package", ids.package_id(idx.proj_id, target_module)
        else:
            to_label, to_id = "Module", ids.module_id(idx.proj_id, target_module)
        yield GraphEdge(
            "IMPORTS", scope_label, {"id": scope_id}, to_label, {"id": to_id},
            {"names": sorted(project[target_module]), "resolution": "resolved"},
        )
    for target_module in sorted(external):
        eid = ids.extern_module_key(target_module)
        if eid not in idx.emitted_externs:
            idx.emitted_externs.add(eid)
            yield GraphNode("Module", {"id": eid}, {
                "name": target_module.rsplit(".", 1)[-1],
                "dotted_path": target_module,
                "resolved": False,
                "origin": _origin(target_module),
                "linkage_key": eid,
            })
        yield GraphEdge(
            "IMPORTS", scope_label, {"id": scope_id}, "Module", {"id": eid},
            {"names": sorted(external[target_module]), "resolution": "external"},
        )


# -- classes -----------------------------------------------------------------------


def _emit_class(cls: RawClass, scope_id: str, scope_dotted: str, container_id: str, container_label: str, container_rel: str, ctx: _ModCtx) -> Iterator[GraphNode | GraphEdge]:
    cls_id = ids.class_id(scope_id, cls.name)
    qname = f"{scope_dotted}.{cls.name}"

    resolved_bases: list[tuple[str, str]] = []
    unresolved_bases: list[str] = []
    for base in cls.bases:
        target = _resolve_type(base, scope_dotted, ctx)
        if target:
            resolved_bases.append(target)
        else:
            unresolved_bases.append(base)
    ctx.idx.parents[qname] = [bq for bq, _ in resolved_bases]
    mro = _linearize(qname, ctx.idx.parents)

    yield GraphNode(
        "Class",
        {"id": cls_id},
        {
            "name": cls.name,
            "qualified_name": qname,
            "linkage_key": ids.extern_symbol_key(scope_dotted, cls.name),
            "bases": cls.bases,
            "mro": mro,
            "unresolved_bases": unresolved_bases,
            "is_abstract": _is_abstract(cls),
            "is_protocol": any(b in _PROTOCOL_BASES for b in cls.bases),
            "is_dataclass": _is_dataclass(cls),
            "decorators": cls.decorators,
            "source": cls.source,
            "doc": cls.doc,
            "line_start": cls.line_start,
            "line_end": cls.line_end,
        },
    )
    yield GraphEdge(container_rel, container_label, {"id": container_id}, "Class", {"id": cls_id})

    for base_qname, base_id in resolved_bases:
        rel = "IMPLEMENTS" if _looks_like_interface(base_qname) else "EXTENDS"
        props: dict[str, object] = {"resolution": "resolved"}
        if base_qname in mro:
            props["mro_index"] = mro.index(base_qname)
        yield GraphEdge(rel, "Class", {"id": cls_id}, "Class", {"id": base_id}, props)

    for attr in _dedup_attrs(cls):
        yield from _emit_attribute(attr, cls_id, ctx)
    for method in cls.methods:
        yield from _emit_function(method, cls_id, qname, scope_dotted, "HAS_METHOD", cls_id, "Class", ctx)
    for nested in cls.nested:
        yield from _emit_class(nested, cls_id, qname, cls_id, "Class", "HAS_NESTED_CLASS", ctx)


def _emit_attribute(attr: RawAttribute, cls_id: str, ctx: _ModCtx) -> Iterator[GraphNode | GraphEdge]:
    attr_id = ids.attribute_id(cls_id, attr.name)
    type_str, source = _type_of(attr.annotation, attr.name_line, attr.name_col, ctx)
    yield GraphNode(
        "Attribute",
        {"id": attr_id},
        {"name": attr.name, "type": type_str, "type_source": source, "defined_in": attr.defined_in, "is_classvar": attr.is_classvar},
    )
    yield GraphEdge("HAS_ATTRIBUTE", "Class", {"id": cls_id}, "Attribute", {"id": attr_id})


# -- functions / methods -----------------------------------------------------------


def _resolve_scope_calls(
    calls: list[RawCall], caller_label: str, caller_id: str, ctx: _ModCtx, scope_qname: str
) -> tuple[list[GraphEdge], list[GraphNode], list[str]]:
    """Resolve the calls made from one scope -- a function/method body OR a module's top-level
    code -- into CALLS edges originating at `caller_id`, the external placeholder nodes those
    calls introduce (ADR 0004, deduped via `idx.emitted_externs`), and the unresolved simple-name
    call texts (jedi engine only, for the node's `unresolved_calls` property). Shared by
    `_emit_function` and `_emit_module` so a top-level call and a function-body call resolve
    identically; only the caller node (`caller_label`/`caller_id`) differs."""
    call_edges: list[GraphEdge] = []
    external_nodes: list[GraphNode] = []
    unresolved_calls: list[str] = []
    for call in calls:
        res = _resolve_call(call, ctx, scope_qname)
        if res is None:
            if ctx.oracle is not None and call.is_simple_name:
                unresolved_calls.append(_call_text(call))
            continue
        if isinstance(res, _External):
            eid = ids.extern_symbol_key(res.qualifier, res.name)
            if eid not in ctx.idx.emitted_externs:
                ctx.idx.emitted_externs.add(eid)
                external_nodes.append(_external_symbol_node(res))
            call_edges.append(
                GraphEdge(
                    "CALLS", caller_label, {"id": caller_id}, res.label, {"id": eid},
                    {"resolution": "external", "receiver_type": None, "argument_types": [], "type_arguments": []},
                )
            )
            continue
        call_edges.append(
            GraphEdge(
                "CALLS", caller_label, {"id": caller_id}, res.label, {"id": res.id},
                {"resolution": res.resolution, "receiver_type": res.receiver_type, "argument_types": [], "type_arguments": []},
            )
        )
    return call_edges, external_nodes, unresolved_calls


def _emit_function(fn: RawFunction, scope_id: str, qual_prefix: str, scope_dotted: str, container_rel: str, container_id: str, container_label: str, ctx: _ModCtx) -> Iterator[GraphNode | GraphEdge]:
    is_method = container_label == "Class"
    param_names = [p.name for p in fn.params]
    fn_id = ids.method_id(scope_id, fn.name, param_names) if is_method else ids.function_id(scope_id, fn.name, param_names)
    label = "Method" if is_method else "Function"
    annotated_ret = fn.returns is not None

    # Resolve calls first so `unresolved_calls` can ride on the node we're about to emit.
    # A resolved call edges to the project callable; an _External call edges to a placeholder
    # (ADR 0004) -- surfacing stdlib/builtin/third-party and not-yet-ingested sibling symbols.
    call_edges, external_nodes, unresolved_calls = _resolve_scope_calls(fn.calls, label, fn_id, ctx, f"{qual_prefix}.{fn.name}")

    props: dict[str, object] = {
        "name": fn.name,
        "qualified_name": f"{qual_prefix}.{fn.name}",
        "linkage_key": ids.extern_symbol_key(qual_prefix, fn.name),
        "signature": _signature(fn),
        "return_type": fn.returns if annotated_ret else "Unknown",
        "return_type_source": "annotated" if annotated_ret else "unresolved",
        "is_async": fn.is_async,
        "is_generator": fn.is_generator,
        "decorators": fn.decorators,
        "unresolved_calls": unresolved_calls,
        "source": fn.source,
        "doc": fn.doc,
        "line_start": fn.line_start,
        "line_end": fn.line_end,
    }
    if is_method:
        props.update(kind=_method_kind(fn), is_abstract=_method_is_abstract(fn), overrides=None)
    yield GraphNode(label, {"id": fn_id}, props)
    yield from external_nodes
    yield GraphEdge(container_rel, container_label, {"id": container_id}, label, {"id": fn_id})

    for i, p in enumerate(fn.params):
        param_id = ids.parameter_id(fn_id, i, p.name)
        type_str, source = _type_of(p.annotation, p.name_line, p.name_col, ctx)
        yield GraphNode(
            "Parameter",
            {"id": param_id},
            {"name": p.name, "type": type_str, "type_source": source, "kind": p.kind, "position": p.position, "has_default": p.has_default},
        )
        yield GraphEdge("HAS_PARAMETER", label, {"id": fn_id}, "Parameter", {"id": param_id})

    if annotated_ret:
        target = _resolve_type(fn.returns, scope_dotted, ctx)
        if target:
            yield GraphEdge("RETURNS", label, {"id": fn_id}, "Class", {"id": target[1]}, {"resolution": "resolved"})

    yield from call_edges

    # Nested functions (closures, factory-/decorator-registered handlers) are first-class nodes, so
    # calls inside them aren't lost and `who_calls` finds them. Each is scoped under this function's
    # id/qname and emitted as a Function (never a Method) regardless of whether this scope is a
    # function or a method -- so the container is this node via HAS_NESTED_FUNCTION.
    for nested in fn.nested:
        yield from _emit_function(
            nested, fn_id, f"{qual_prefix}.{fn.name}", scope_dotted,
            "HAS_NESTED_FUNCTION", fn_id, label, ctx,
        )


# -- resolution helpers ------------------------------------------------------------


def _type_of(annotation: str | None, line: int, col: int, ctx: _ModCtx) -> tuple[str, str]:
    """(type string, type_source). Annotation wins; else the oracle infers; else unresolved.
    The ast engine (oracle=None) therefore only ever yields 'annotated' or 'unresolved'."""
    if annotation is not None:
        return annotation, "annotated"
    if ctx.oracle is not None:
        inferred = ctx.oracle.infer_type(ctx.module_path, line, col)
        if inferred:
            return inferred, "inferred"
    return "Unknown", "unresolved"


def _resolve_type(expr: str, scope_dotted: str, ctx: _ModCtx) -> tuple[str, str] | None:
    """Bind a base/return type expression to a project-local class -> (qname, class_id).
    Static (import/same-module/qualified); external types return None (properties only)."""
    name = expr.split("[", 1)[0].strip()
    if name in ctx.import_map:
        target = ctx.import_map[name].target
        if target in ctx.idx.class_by_qname:
            return target, ctx.idx.class_by_qname[target]
    same_module = f"{scope_dotted}.{name}"
    if same_module in ctx.idx.class_by_qname:
        return same_module, ctx.idx.class_by_qname[same_module]
    if name in ctx.idx.class_by_qname:
        return name, ctx.idx.class_by_qname[name]
    if "." in name:
        head, _, tail = name.rpartition(".")
        if head in ctx.import_map and ctx.import_map[head].is_module:
            qn = f"{ctx.import_map[head].target}.{tail}"
            if qn in ctx.idx.class_by_qname:
                return qn, ctx.idx.class_by_qname[qn]
    return None


def _project_defines(full_name: str, ctx: _ModCtx) -> bool:
    """A dotted name that names a project entity the call model doesn't turn into an edge (a class
    constructor, a module/package) -- so it must NOT be mistaken for an external placeholder."""
    return (full_name in ctx.idx.class_by_qname
            or full_name in ctx.idx.module_dotteds
            or full_name in ctx.idx.packages)


def _split_qualifier(full_name: str) -> tuple[str, str]:
    qualifier, _, name = full_name.rpartition(".")
    return qualifier, name


def _enclosing_scopes(scope_qname: str, module_dotted: str) -> list[str]:
    """The enclosing-function qname prefixes to try for a lexical (simple-name) lookup, innermost
    first, DOWN TO BUT EXCLUDING the module scope (which the caller checks separately). For a call in
    `m.outer.tool` this yields `["m.outer.tool", "m.outer"]`, so `tool`'s own nested defs, then its
    siblings / the enclosing `outer` / a recursive self-call, resolve before module/import fallbacks.
    A module-scope caller (`scope_qname == module_dotted`) yields no inner scopes."""
    scopes: list[str] = []
    cur = scope_qname
    while cur and cur != module_dotted and "." in cur:
        scopes.append(cur)
        cur = cur.rsplit(".", 1)[0]
    return scopes


def _external_or_none(qualifier: str, name: str, ctx: _ModCtx) -> _External | None:
    """A placeholder is made only for a symbol whose *container* is outside this project (stdlib/
    third-party/another repo). A missing symbol inside a project module/class is an intra-project
    dangling reference, not an external one -- drop it (ADR 0004), so a symbol that only appears
    later doesn't leave a stale `extern://` node behind an incremental resync."""
    if (qualifier in ctx.idx.module_dotteds
            or qualifier in ctx.idx.packages
            or qualifier in ctx.idx.class_by_qname):
        return None
    return _External(qualifier, name)


def _constructor_call(class_qname: str, ctx: _ModCtx) -> _Call | None:
    """Instantiating a project class runs its constructor. Redirect the call to the class's own
    `__init__` method when it defines one; otherwise (a default constructor, or one purely inherited
    from a base -- no local `__init__` to name) edge to the CLASS node itself, so that every
    project-class instantiation (`raise SomeError(...)`, `Topology()`, a subclass that only inherits
    `__init__`) is still captured for `who_calls`/`impact_of`. `resolution="constructor"` marks the
    edge as object construction (not a literal textual call to `__init__`); `receiver_type` is the
    class's simple name, mirroring a receiver-typed method call."""
    simple = class_qname.rsplit(".", 1)[-1]
    ctor = ctx.idx.callable_by_qname.get(f"{class_qname}.__init__")
    if ctor is not None:
        return _Call(ctor[0], ctor[1], "constructor", simple)
    cls_id = ctx.idx.class_by_qname.get(class_qname)
    if cls_id is None:  # not a known project class (shouldn't happen -- callers gate on class_by_qname)
        return None
    return _Call(cls_id, "Class", "constructor", simple)


def _resolve_call(call: RawCall, ctx: _ModCtx, scope_qname: str) -> _Call | _External | None:
    """Resolve a call. Returns a _Call (bound to a project callable), an _External (nameable but
    not defined in this ingest -> a placeholder node, ADR 0004), or None (dynamic / unnameable).
    `scope_qname` is the calling scope's qname (the function's own qname, or the module dotted for
    module-scope code) -- used by the ast path for lexical (enclosing-scope) name resolution."""
    if ctx.oracle is not None:
        full_name = ctx.oracle.resolve_call(ctx.module_path, call.line, call.col)
        if not full_name:
            # The oracle found nothing; still surface a bare builtin call (len(), getattr()).
            if call.is_simple_name and call.name in _BUILTINS:
                return _External("builtins", call.name)
            return None
        target = ctx.idx.callable_by_qname.get(full_name)
        if target is None:
            if full_name in ctx.idx.class_by_qname:
                return _constructor_call(full_name, ctx)  # instantiation -> the class's __init__
            if _project_defines(full_name, ctx):
                return None  # a project module/class w/o a ctor the call model doesn't edge -> drop
            qualifier, name = _split_qualifier(full_name)
            return _external_or_none(qualifier, name, ctx)
        tid, tlabel = target
        via_member = not call.is_simple_name
        # Derive the receiver type from the target's own qname (e.g.
        # "app.db.Connection.execute" -> "Connection") rather than a second inference.
        receiver_type = full_name.rsplit(".", 2)[-2] if via_member and tlabel == "Method" else None
        return _Call(tid, tlabel, "inferred" if via_member else "resolved", receiver_type)

    # ast path: import-bound or same-module direct name calls -> project functions first.
    if call.is_simple_name:
        # Lexical (enclosing-function) scopes first: a nested def shadows a module/imported name, and a
        # call to a sibling nested function / the enclosing function / a recursive self-call resolves
        # here. (The jedi path gets this from inference; the ast path must walk the scope chain.)
        for prefix in _enclosing_scopes(scope_qname, ctx.module_dotted):
            hit = ctx.idx.callable_by_qname.get(f"{prefix}.{call.name}")
            if hit and hit[1] == "Function":
                return _Call(hit[0], "Function", "resolved", None)
        if call.name in ctx.import_map:
            hit = ctx.idx.callable_by_qname.get(ctx.import_map[call.name].target)
            if hit and hit[1] == "Function":
                return _Call(hit[0], "Function", "resolved", None)
        hit = ctx.idx.callable_by_qname.get(f"{ctx.module_dotted}.{call.name}")
        if hit and hit[1] == "Function":
            return _Call(hit[0], "Function", "resolved", None)
        # Instantiation of a statically-nameable project class -> its constructor (same redirect
        # as the jedi path): an import-bound class (`from pkg import Cls; Cls()`) or a same-module
        # one (`class Cls: ...; Cls()`). A class with no local __init__ yields None (no edge).
        if call.name in ctx.import_map and ctx.import_map[call.name].target in ctx.idx.class_by_qname:
            return _constructor_call(ctx.import_map[call.name].target, ctx)
        same_module_cls = f"{ctx.module_dotted}.{call.name}"
        if same_module_cls in ctx.idx.class_by_qname:
            return _constructor_call(same_module_cls, ctx)
        # not a project function -> external when we can name it (import-bound, or a builtin).
        if call.name in ctx.import_map:
            target = ctx.import_map[call.name].target
            if _project_defines(target, ctx):
                return None
            qualifier, name = _split_qualifier(target)
            return _external_or_none(qualifier, name, ctx)
        if call.name in _BUILTINS:
            return _External("builtins", call.name)
        return None
    if call.receiver and call.receiver in ctx.import_map:
        # `receiver.name(...)` where `receiver` is an imported name. The imported thing's dotted
        # target is the container of `name` regardless of whether it's a module (`import pkg.mod as
        # m` / `from pkg import mod`) or a class (`from pkg import Cls`): the linkage key is
        # `<target>::<name>` either way -- a module function keys on the module dotted, a class
        # method keys on the class qname, both == the import target. So we do NOT gate on
        # `is_module` (which is False for every `from X import Y`, the common `from pkg import
        # submodule; submodule.fn()` idiom that otherwise goes undetected -> no cross-repo edge).
        target_dotted = ctx.import_map[call.receiver].target
        full = f"{target_dotted}.{call.name}"
        hit = ctx.idx.callable_by_qname.get(full)
        if hit and hit[1] == "Function":
            return _Call(hit[0], "Function", "resolved", None)
        if full in ctx.idx.class_by_qname:
            return _constructor_call(full, ctx)  # `mod.Cls()` -> the class's __init__
        if _project_defines(full, ctx):
            return None
        return _external_or_none(target_dotted, call.name, ctx)
    return None


def _external_symbol_node(ext: _External) -> GraphNode:
    """A placeholder node for an unresolved reference, keyed by its linkage so a later ingest of
    the defining repo merges onto it (ADR 0004). Shape mirrors a real callable so the same
    queries reach it; `resolved:false` + `origin` mark it."""
    eid = ids.extern_symbol_key(ext.qualifier, ext.name)
    return GraphNode(ext.label, {"id": eid}, {
        "name": ext.name,
        "qualified_name": f"{ext.qualifier}.{ext.name}" if ext.qualifier else ext.name,
        "module": ext.qualifier,
        "resolved": False,
        "origin": _origin(ext.qualifier),
        "linkage_key": eid,
    })


def _call_text(call: RawCall) -> str:
    return f"{call.receiver}.{call.name}()" if call.receiver else f"{call.name}()"


def _linearize(qname: str, parents: dict[str, list[str]]) -> list[str]:
    """Best-effort MRO over resolved local ancestors (depth-first, de-duplicated),
    terminated by `object`. Not full C3; honest for the common single-inheritance case."""
    order: list[str] = []

    def visit(q: str):
        if q in order:
            return
        order.append(q)
        for parent in parents.get(q, []):
            visit(parent)

    visit(qname)
    order.append("object")
    return order


# -- classification helpers --------------------------------------------------------


def _dedup_attrs(cls: RawClass) -> list[RawAttribute]:
    by_name: dict[str, RawAttribute] = {}
    ordered: list[str] = []
    candidates = list(cls.class_attrs) + [a for method in cls.methods for a in method.self_attrs]
    for attr in candidates:
        existing = by_name.get(attr.name)
        if existing is None:
            by_name[attr.name] = attr
            ordered.append(attr.name)
        elif existing.annotation is None and attr.annotation is not None:
            by_name[attr.name] = attr
        elif existing.defined_in != "__init__" and attr.defined_in == "__init__":
            by_name[attr.name] = attr
    return [by_name[n] for n in ordered]


def _looks_like_interface(base_qname: str) -> bool:
    simple = base_qname.rsplit(".", 1)[-1]
    return simple in {"Protocol", "ABC"} or base_qname in _PROTOCOL_BASES or base_qname in _ABC_BASES


def _is_abstract(cls: RawClass) -> bool:
    if any(b in _ABC_BASES or b.rsplit(".", 1)[-1] == "ABC" for b in cls.bases):
        return True
    return any(_method_is_abstract(m) for m in cls.methods)


def _is_dataclass(cls: RawClass) -> bool:
    if any("dataclass" in d for d in cls.decorators):
        return True
    tails = {b.split("[", 1)[0].rsplit(".", 1)[-1] for b in cls.bases}
    return bool(tails & {"BaseModel", "TypedDict", "NamedTuple"})


def _method_kind(fn: RawFunction) -> str:
    decs = fn.decorators
    if any(d.rsplit(".", 1)[-1] == "staticmethod" for d in decs):
        return "static"
    if any(d.rsplit(".", 1)[-1] == "classmethod" for d in decs):
        return "class"
    if any(d.rsplit(".", 1)[-1] == "property" or d.endswith((".setter", ".getter", ".deleter")) for d in decs):
        return "property"
    return "instance"


def _method_is_abstract(fn: RawFunction) -> bool:
    return any("abstractmethod" in d for d in fn.decorators)


def _signature(fn: RawFunction) -> str:
    parts = []
    for p in fn.params:
        prefix = {"var_positional": "*", "var_keyword": "**"}.get(p.kind, "")
        parts.append(f"{prefix}{p.name}: {p.annotation}" if p.annotation else f"{prefix}{p.name}")
    ret = f" -> {fn.returns}" if fn.returns else ""
    return f"{fn.name}({', '.join(parts)}){ret}"
