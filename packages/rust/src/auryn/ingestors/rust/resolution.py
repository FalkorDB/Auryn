"""Project-wide pass (phase 2): turn the per-file RawFile list into graph nodes/edges, applying
the cross-file/cross-module decisions a single file can't -- materializing the `mod` tier,
re-attaching each `impl`/`trait` block's methods to the type they belong to, resolving
IMPLEMENTS/EXTENDS and parameter/return/field type references to project types, and best-effort
call resolution.

Rust differs from the Java/C# template in one structural way this pass handles: a type's methods
do not live inside the type -- they live in separate `impl` blocks (an `impl Circle` and an
`impl Shape for Circle` both bind methods to `Circle`, and either may sit in another file). So
methods are indexed by their resolved owning type, and a method's receiver-typed calls read that
owning struct's fields (plus the method's own params) to type the receiver.

Name-based only -- this is the fast tree-sitter engine, so every CALLS edge is stamped
`resolution="name"`. A future deep rust-analyzer/rustc pass would re-emit the same node ids with
a stronger provenance (the two-phase model). Ambiguous or unresolved references produce no edge
rather than a wrong guess -- the same trade-off as the Java/C#/PL-SQL ingestors."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field

from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.ingestors.rust import cargo, ids
from auryn.ingestors.rust.raw_model import RawConst, RawField, RawFunction, RawType

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# ADR 0004 -- classifying an unresolved (nameable) call. A path whose leading segment is one of
# these std crates is a terminal stdlib symbol (origin="stdlib"); anything else external is
# origin="external" (the candidate a sibling repo could later define). A path rooted at one of
# `_PROJECT_PATH_ROOTS` is project-relative, so a miss there is intra-project dangling, not
# external -- left dropped (no placeholder).
_STDLIB_ROOTS = frozenset({"std", "core", "alloc"})
_PROJECT_PATH_ROOTS = frozenset({"crate", "self", "super", "Self"})
# Tokens that appear in type text but never name a project type (primitives + type keywords). A
# name that isn't a project type simply won't be in the type_index, so this is only a fast skip.
_NON_TYPE_TOKENS = frozenset(
    {
        "i8", "i16", "i32", "i64", "i128", "isize",
        "u8", "u16", "u32", "u64", "u128", "usize",
        "f32", "f64", "bool", "char", "str",
        "dyn", "impl", "mut", "ref", "as", "where", "Self", "self",
    }
)


@dataclass
class _TypeEntry:
    type_id: str
    kind: str  # "Struct" | "Enum" | "Trait"
    name: str
    scope_id: str
    scope_label: str  # "Module" | "Project"
    file_id: str
    raw: RawType


@dataclass
class _Callable:
    id: str
    label: str  # "Function" | "Method"
    scope_id: str
    self_type_id: str | None  # owning type id for a method; None for a free function
    owner_label: str | None  # owning type kind for HAS_METHOD; None for a free function
    owner_id: str | None
    raw: RawFunction
    file_id: str = ""  # the File that DEFINES this callable (per-file provenance for delta-sync, ADR 0005)
    impl_generics: tuple[str, ...] = ()  # generics from the enclosing impl block (`impl<T> Foo<T>`),
    # merged with the method's own + owning-type generics when flagging dynamic dispatch (#792)


@dataclass
class _External:
    """ADR 0004: a nameable but unresolved callee -- a placeholder keyed by its cross-repo
    linkage. `eid` is `extern://rust/<qualified>` (the placeholder node id AND its linkage_key),
    so a later ingest of the defining repo heals the call onto the real definition."""
    eid: str
    label: str  # "Function" (path call `a::b::f()`) | "Method" (`recv.m()` on an external type)
    name: str  # the short callee name
    qualified: str  # the `::`-joined path a consumer wrote
    origin: str  # "stdlib" | "external"
    extra: dict = field(default_factory=dict)  # extra CALLS-edge props (e.g. receiver_type)


@dataclass
class _Collected:
    # Cargo tier (issue #5). Crate/Package are aggregation nodes authored by their member files --
    # path-derived props + File-DEFINES provenance, exactly like Module -- so delta-sync GCs them by
    # refcount. (Manifest-only facts -- edition/version/deps -- are layered on by git_sync's reconcile.)
    package_nodes: dict = field(default_factory=dict)  # package_id -> (name, dir)
    crate_nodes: dict = field(default_factory=dict)    # crate_id -> (name, kind, dir, package_id)
    crate_defs: set = field(default_factory=set)       # (file_id, crate_id): File-DEFINES-Crate
    package_defs: set = field(default_factory=set)     # (file_id, package_id): File-DEFINES-Package
    module_ids: set = field(default_factory=set)
    module_nodes: list = field(default_factory=list)  # (mid, segment, dotted, is_declaration)
    module_edges: list = field(default_factory=list)  # (rel, parent_label, parent_id, mid)
    module_defs: set = field(default_factory=set)  # (file_id, mid): File-DEFINES-Module provenance,
    #   one per declaring file (a `mod x;`/`mod x{}` occurrence), so the delta-sync engine can GC a
    #   Module whose last declaring file is removed/renamed -- exactly as it does for types/fns/consts.
    types: list = field(default_factory=list)  # _TypeEntry
    funcs: list = field(default_factory=list)  # (scope_id, scope_label, file_id, RawFunction)
    consts: list = field(default_factory=list)  # (scope_id, scope_label, file_id, RawConst)
    impls: list = field(default_factory=list)  # (file_id, RawImpl)


@dataclass
class RustGraphIndex:
    """Rust resolution tables seeded from the GRAPH for the NON-emit files (ADR 0005), so
    `resolve_incremental` can resolve an emit file's cross-module/crate refs/calls without
    re-parsing the project. Types come back as lightweight `_TypeEntry` stubs (scope/file blank --
    only id/kind/name + fields are needed to point an edge at a non-emit target, or to type a
    receiver whose method an emit `impl` provides). Reconstructed by `rust.incremental.read_rust_index`."""

    types: list = field(default_factory=list)          # _TypeEntry stubs (non-emit types)
    func_by_name: dict = field(default_factory=dict)   # name -> [(func_id, scope_id)]
    method_by_name: dict = field(default_factory=dict)  # name -> [(owner_type_id, method_id, owner_name)]
    # Segments of project MODULES declared by surviving (non-emit) files. The `_external_path` guard
    # drops a call whose leading segment names a project module (an intra-project ref), so this set must
    # span the WHOLE project -- emit modules alone (c.module_nodes) would miss a module declared in a
    # non-emit file (e.g. clap's `mod str`), wrongly materializing `str::from_utf8` as an extern the full
    # path drops (incremental != full).
    project_module_names: set = field(default_factory=set)


def make_type_stub(type_id: str, kind: str, name: str, fields: list, type_parameters=()) -> _TypeEntry:
    """A lightweight `_TypeEntry` for a NON-emit type the seed provides: id/kind/name (to point an
    edge at it) + `fields` (name/type) so an emit method whose receiver is this type can still resolve
    field-typed calls + `type_parameters` so an emit method on this (generic) receiver still flags
    dynamic dispatch exactly as a full ingest would (incremental == full, #792). Never emitted."""
    return _TypeEntry(type_id=type_id, kind=kind, name=name, scope_id="", scope_label="",
                      file_id="", raw=RawType(kind=kind, name=name, fields=fields,
                                              type_parameters=list(type_parameters)))


def resolve(raw_files, project_id: str) -> Iterator[GraphNode | GraphEdge]:
    yield from _resolve(raw_files, project_id, seed=None)


def resolve_incremental(emit_raws, project_id: str, index: RustGraphIndex) -> list:
    """Emit graph items for ONLY `emit_raws`, resolving cross-module/crate refs/calls against
    `index` (graph-seeded non-emit symbols) merged with the emit files' own. The blast-radius
    analogue of resolve() (ADR 0005): same emitters, indices seeded rather than whole-project."""
    return list(_resolve(emit_raws, project_id, seed=index))


@dataclass(frozen=True)
class CallableLocation:
    """Where a project callable lives, keyed by its graph node id -- the join the deep (rust-analyzer
    / SCIP) pass needs: SCIP reports a resolved call by (file, line); this maps that back to the fast
    pass's Function/Method node, and its [line_start, line_end] span attributes a call site to the
    enclosing callable by containment."""

    id: str
    label: str  # "Function" | "Method" -- the node label the deep CALLS edge must point at
    rel_path: str
    line_start: int  # 1-based inclusive (as RawFunction records them)
    line_end: int
    name: str
    called_names: frozenset[str]  # callee simple names the fast parse saw as CALLS in this body --
    # lets the deep pass disambiguate same-line callers and recompute unresolved_calls from what SCIP
    # actually resolved (macro-internal calls aren't here; they're counted, not parsed as RawCalls)


def callable_locations(raw_files, project_id: str) -> list[CallableLocation]:
    """Every project callable's (graph id, file, 1-based line span, name). Reuses the SAME collection
    and id functions as `resolve()` so the ids are byte-identical -- deep-pass CALLS edges built from
    these land on the real nodes the fast pass authored (ids embed only syntactic signatures, never
    resolved types, so this needs no type inference)."""
    raw_files = sorted(raw_files, key=lambda rf: _norm(rf.path))
    c = _Collected()
    fid_to_path: dict[str, str] = {}
    for rf in raw_files:
        fid = ids.file_id(project_id, rf.path)
        fid_to_path[fid] = _norm(rf.path)
        ct = cargo.crate_target_for_path(rf.path)
        cid = ids.crate_id(project_id, ct.package_dir, ct.kind, ct.name)
        _collect(rf, cid, "Crate", cid, fid, "", c)

    type_index: dict[str, list[_TypeEntry]] = defaultdict(list)
    for e in c.types:
        type_index[e.name].append(e)

    out: list[CallableLocation] = []

    def add(node_id: str, label: str, file_id: str, raw) -> None:
        out.append(CallableLocation(node_id, label, fid_to_path.get(file_id, ""),
                                    raw.line_start, raw.line_end, raw.name,
                                    frozenset(c.name for c in raw.calls)))

    for e in c.types:  # trait-declared methods bind to the Trait itself (mirror of _resolve)
        if e.kind != "Trait":
            continue
        for m in e.raw.trait_methods:
            add(ids.method_id(e.type_id, m.name, [p.type for p in m.parameters]), "Method", e.file_id, m)
    for impl_file_id, impl in c.impls:
        owner = _resolve_target(impl.type_name, type_index)
        if owner is None:
            continue
        for m in impl.methods:
            add(ids.method_id(owner.type_id, m.name, [p.type for p in m.parameters], trait_name=impl.trait_name),
                "Method", impl_file_id, m)
    for scope_id, _scope_label, fid, fn in c.funcs:
        add(ids.function_id(scope_id, fn.name, [p.type for p in fn.parameters]), "Function", fid, fn)
    return out


def _resolve(raw_files, project_id: str, seed: "RustGraphIndex | None") -> Iterator[GraphNode | GraphEdge]:
    # Stable path order so a symbol DEFINED in several files (multi-binary crates: several
    # crate-root `fn main`) reduces to an order-INDEPENDENT winner -- identical here and in
    # resolve_incremental's subset, the precondition for incremental == full (ADR 0005).
    raw_files = sorted(raw_files, key=lambda rf: _norm(rf.path))

    # -- files + Cargo tier (issue #5) -----------------------------------------------------
    # Each file is mapped BY ITS PATH ALONE (cargo.crate_target_for_path -- no manifest/source) to
    # the crate it compiles into; that crate is the file's SCOPE ROOT, so two crates' identically
    # named items no longer collide. Path-only is what keeps a full ingest and an incremental resync
    # computing the identical ids (the delta callbacks have no source) -- the precondition for the
    # differential oracle. Package/Crate are authored here with id-derived props + File-DEFINES
    # provenance, so delta-sync refcount-GCs them like Module (manifest facts are layered on later).
    file_ids: dict[str, str] = {}
    crate_id_of: dict[str, str] = {}        # file path -> owning crate id (the scope root)
    crate_name_of_fid: dict[str, str] = {}  # file id   -> owning crate name (for ADR 0004 linkage)
    c = _Collected()
    for rf in raw_files:
        fid = ids.file_id(project_id, rf.path)
        file_ids[rf.path] = fid
        yield GraphNode("File", {"id": fid}, {"name": _basename(rf.path), "path": _norm(rf.path)})
        yield GraphEdge("HAS_FILE", "Project", {"id": project_id}, "File", {"id": fid})

        ct = cargo.crate_target_for_path(rf.path)
        pid = ids.package_id(project_id, ct.package_dir)
        cid = ids.crate_id(project_id, ct.package_dir, ct.kind, ct.name)
        # Display/linkage name: the target's crate name, but the REPO-ROOT crate (no package dir, so
        # no manifest to read a name from) takes the project name -- the best guess a cross-repo
        # consumer would write, and nicer than the "crate" layout placeholder. (The id slug keeps the
        # path-derived form; git_sync's reconcile overrides the name from `[package].name`.)
        cname = ct.name if ct.package_dir else project_id.split("://", 1)[-1]
        crate_id_of[rf.path] = cid
        crate_name_of_fid[fid] = cname
        c.package_nodes.setdefault(pid, (_package_name(ct.package_dir, project_id), ct.package_dir))
        c.crate_nodes.setdefault(cid, (cname, ct.kind, ct.package_dir, pid))
        c.crate_defs.add((fid, cid))
        c.package_defs.add((fid, pid))

    # -- collect every scope's items (crate-root scope = Crate; each `mod` = Module) --------
    for rf in raw_files:
        cid = crate_id_of[rf.path]
        _collect(rf, cid, "Crate", cid, file_ids[rf.path], "", c)

    # Cargo-tier nodes + backbone + provenance. Props are id-derived (name/kind follow from the id),
    # so a crate/package authored by several files reduces to one identical node -- the same
    # precondition the Module tier relies on for exclusion from the co-DEFINER closure.
    for pid in sorted(c.package_nodes):
        pname, pdir = c.package_nodes[pid]
        yield GraphNode("Package", {"id": pid}, {"name": pname, "qualified_name": pdir or pname, "path": pdir})
        yield GraphEdge("HAS_PACKAGE", "Project", {"id": project_id}, "Package", {"id": pid})
    for cid in sorted(c.crate_nodes):
        cname, ckind, _cdir, pid = c.crate_nodes[cid]
        yield GraphNode("Crate", {"id": cid}, {"name": cname, "kind": ckind, "qualified_name": f"{cname} ({ckind})"})
        yield GraphEdge("HAS_TARGET", "Package", {"id": pid}, "Crate", {"id": cid})
    for fid, pid in sorted(c.package_defs):
        yield GraphEdge("DEFINES", "File", {"id": fid}, "Package", {"id": pid})
    for fid, cid in sorted(c.crate_defs):
        yield GraphEdge("DEFINES", "File", {"id": fid}, "Crate", {"id": cid})

    for mid, segment, dotted, _is_decl in c.module_nodes:
        # Props are FULLY id-derived (name/qualified_name follow from `mid`), so a module authored by
        # several files reduces to the identical node no matter which wins -- the precondition for
        # excluding Module from the co-DEFINER closure (RUST_SPEC.aggregation_labels). `is_declaration`
        # (author-dependent: `mod x;` vs `mod x {}`) is intentionally NOT stored on the graph node.
        yield GraphNode("Module", {"id": mid}, {"name": segment, "qualified_name": dotted})
    for rel, parent_label, parent_id, mid in c.module_edges:
        yield GraphEdge(rel, parent_label, {"id": parent_id}, "Module", {"id": mid})
    # File→Module provenance (ADR 0005): the ownership handle the delta-sync engine GCs a Module by,
    # one edge per declaring file (co-contributor closure). Emitted in both full and incremental
    # (this is `_resolve`), so a from-scratch graph carries it too and the two stay byte-identical.
    for fid, mid in sorted(c.module_defs):
        yield GraphEdge("DEFINES", "File", {"id": fid}, "Module", {"id": mid})

    # -- indexes ---------------------------------------------------------------------------
    # Per-module crate-relative path, so a real callable's cross-repo linkage_key matches what a
    # consumer writes (ADR 0004): `crate::mod::fn` for a free function, `Type::method` for a method
    # -- where `crate` is now the callable's OWN crate name (crate_name_of_fid), not the project
    # name, so a workspace's distinct crates get distinct linkage keys. project_modules lets the
    # placeholder guard tell an external crate path from a leading project-module segment.
    dotted_by_id = {mid: dotted for mid, _segment, dotted, _decl in c.module_nodes}
    project_modules = {segment for _mid, segment, _dotted, _decl in c.module_nodes}
    # Seed the project-module set with modules declared by NON-emit files, so the `_external_path` guard
    # recognizes an intra-project module (e.g. clap's `mod str`) declared outside the emit set -- else the
    # incremental materializes a `str::from_utf8` placeholder the full path drops (incremental != full).
    if seed is not None:
        project_modules |= seed.project_module_names
    emitted_externs: set[str] = set()  # placeholder ids already emitted this pass (dedup)

    type_index: dict[str, list[_TypeEntry]] = defaultdict(list)
    for e in c.types:
        type_index[e.name].append(e)
    type_by_id = {e.type_id: e for e in c.types}
    type_fields = {
        e.type_id: {f.name: f.type for f in e.raw.fields if f.type}
        for e in c.types
        if e.kind == "Struct"
    }

    # Merge the graph seed's NON-emit types into the resolution indexes (emit types win: a seed
    # stub is only added when the id isn't already an emit type). Stubs are resolution targets only
    # -- never in c.types, so pass 1/2 never re-emit them (ADR 0005).
    if seed is not None:
        for e in seed.types:
            if e.type_id in type_by_id:
                continue
            type_index[e.name].append(e)
            type_by_id[e.type_id] = e
            if e.kind == "Struct":
                type_fields.setdefault(e.type_id, {f.name: f.type for f in e.raw.fields if f.type})

    callables: list[_Callable] = []
    method_index: dict[str, list[tuple[str, str, str]]] = defaultdict(list)  # name -> (owner_id, mid, owner_name)
    function_index: dict[str, list[tuple[str, str]]] = defaultdict(list)  # name -> (func_id, scope_id)

    # trait-declared methods bind to the Trait itself.
    for e in c.types:
        if e.kind != "Trait":
            continue
        for m in e.raw.trait_methods:
            mid = ids.method_id(e.type_id, m.name, [p.type for p in m.parameters])
            callables.append(_Callable(mid, "Method", e.scope_id, e.type_id, e.kind, e.type_id, m, file_id=e.file_id))
            method_index[m.name].append((e.type_id, mid, e.name))

    # impl-block methods bind to their resolved target type (the impl block's own file DEFINES them).
    impl_methods: list[tuple[_TypeEntry, "object", str]] = []  # (owner, RawImpl, impl_file_id) for IMPLEMENTS pass
    for impl_file_id, impl in c.impls:
        owner = _resolve_target(impl.type_name, type_index)
        if owner is None:
            continue  # impl targets a non-project type -> its methods have no landing node
        impl_methods.append((owner, impl, impl_file_id))
        for m in impl.methods:
            mid = ids.method_id(owner.type_id, m.name, [p.type for p in m.parameters], trait_name=impl.trait_name)
            callables.append(_Callable(mid, "Method", owner.scope_id, owner.type_id, owner.kind, owner.type_id, m,
                                       file_id=impl_file_id, impl_generics=tuple(impl.type_parameters)))
            method_index[m.name].append((owner.type_id, mid, owner.name))

    # free functions.
    for scope_id, scope_label, fid, fn in c.funcs:
        func_id = ids.function_id(scope_id, fn.name, [p.type for p in fn.parameters])
        callables.append(_Callable(func_id, "Function", scope_id, None, None, None, fn, file_id=fid))
        function_index[fn.name].append((func_id, scope_id))

    # Merge the seed's NON-emit functions/methods (seed excludes emit files, so no double node; a
    # colliding symbol's other definer was pulled into the emit set by the co-definer closure).
    if seed is not None:
        for name, entries in seed.func_by_name.items():
            function_index[name].extend(entries)
        for name, entries in seed.method_by_name.items():
            method_index[name].extend(entries)

    # -- pass 1: nodes + structural edges (so pass 2's cross-refs have landing nodes) -------
    # Dedup NODES by id (keep first; files are sorted, so the winner is order-INDEPENDENT). A symbol
    # DEFINED in several files (multi-binary crates: two crate-root `fn main`) thus yields ONE node
    # with deterministic props -- identical in full and incremental. Every definer's EDGES still flow
    # (a DEFINES from each file; CALLS unioned in pass 2), matching a full recompute.
    seen_nodes: set = set()

    def _once(it):
        if isinstance(it, GraphNode):
            k = (it.label, str(it.key["id"]))
            if k in seen_nodes:
                return False
            seen_nodes.add(k)
        return True

    for e in c.types:
        for it in _emit_type(e):
            if _once(it):
                yield it
    # callable NODES are emitted in pass 2 (below), once their calls are resolved -- the node carries an
    # `unresolved_calls` count that is only known after resolution (#792).
    for scope_id, scope_label, file_id, rc in c.consts:
        for it in _emit_const(scope_id, scope_label, file_id, rc):
            if _once(it):
                yield it

    # -- pass 2: hierarchy, type references, calls -----------------------------------------
    for owner, impl, impl_file_id in impl_methods:
        if impl.trait_name:
            trait = _resolve_target(impl.trait_name, type_index)
            if trait is not None and trait.kind == "Trait" and trait.type_id != owner.type_id:
                # Stamp the authoring file: `impl Trait for Type` can live in a file that defines NEITHER
                # the type NOR the trait (Rust's orphan rule allows it anywhere in the crate), and a
                # methodless impl leaves no method node to attribute it. The incremental read-back uses
                # `_src_file` to tell whether the emit set re-authors this edge, so it never deletes an
                # IMPLEMENTS edge whose (unchanged) impl file wasn't re-emitted (see incremental_scoped).
                yield GraphEdge("IMPLEMENTS", owner.kind, {"id": owner.type_id}, "Trait", {"id": trait.type_id},
                                {"_src_file": impl_file_id})
    for e in c.types:
        if e.kind == "Trait":
            for st in e.raw.supertraits:
                target = _resolve_target(st, type_index)
                if target is not None and target.kind == "Trait" and target.type_id != e.type_id:
                    yield GraphEdge("EXTENDS", "Trait", {"id": e.type_id}, "Trait", {"id": target.type_id})
        # struct field type references.
        for f in e.raw.fields:
            fid = ids.field_id(e.type_id, f.name)
            yield from _depends_on("Field", fid, [f.type], type_index, e.type_id)

    for cal in callables:
        cal_crate = crate_name_of_fid.get(cal.file_id) or project_id.split("://", 1)[-1]
        call_items, unresolved_calls = _resolve_calls(cal, type_fields, type_index, function_index,
                                                      method_index, project_modules, emitted_externs, type_by_id)
        for it in _emit_callable_node(cal, _real_linkage(cal, cal_crate, dotted_by_id, type_by_id), unresolved_calls):
            if _once(it):
                yield it
        yield from call_items
        type_texts = [cal.raw.return_type] + [p.type for p in cal.raw.parameters]
        yield from _depends_on(cal.label, cal.id, type_texts, type_index, cal.self_type_id)


# -- collection ----------------------------------------------------------------------------

def _collect(scope, scope_id: str, scope_label: str, root_id: str, file_id: str, dotted: str, c: _Collected) -> None:
    for rt in getattr(scope, "types", []):
        c.types.append(_TypeEntry(
            type_id=ids.type_id(scope_id, rt.kind, rt.name, rt.type_parameters),
            kind=rt.kind, name=rt.name, scope_id=scope_id, scope_label=scope_label,
            file_id=file_id, raw=rt,
        ))
    for fn in getattr(scope, "functions", []):
        c.funcs.append((scope_id, scope_label, file_id, fn))
    for rc in getattr(scope, "consts", []):
        c.consts.append((scope_id, scope_label, file_id, rc))
    for impl in getattr(scope, "impls", []):
        c.impls.append((file_id, impl))
    for m in getattr(scope, "modules", []):
        child_dotted = f"{dotted}.{m.name}" if dotted else m.name
        mid = ids.module_id(root_id, child_dotted)  # root_id is the owning crate id (stays constant
        #   through the recursion); child_dotted accumulates the crate-relative module path.
        c.module_defs.add((file_id, mid))  # provenance from EVERY declaring file (outside the node
        #   dedup): a module declared by several files survives until the LAST is gone (co-contributor).
        if mid not in c.module_ids:
            c.module_ids.add(mid)
            c.module_nodes.append((mid, m.name, child_dotted, m.is_declaration))
            # A crate-root module hangs off the Crate (HAS_MODULE); a nested one off its parent Module.
            rel = "HAS_SUBMODULE" if scope_label == "Module" else "HAS_MODULE"
            c.module_edges.append((rel, scope_label, scope_id, mid))
        _collect(m, mid, "Module", root_id, file_id, child_dotted, c)


# -- pass 1 emitters -----------------------------------------------------------------------

def _emit_type(e: _TypeEntry) -> Iterator[GraphNode | GraphEdge]:
    rt = e.raw
    props = {
        "name": rt.name,
        "visibility": rt.visibility,
        "type_parameters": list(rt.type_parameters),
        "source": rt.source,
        "doc": rt.doc,
        "line_start": rt.line_start,
        "line_end": rt.line_end,
    }
    if e.kind == "Trait":
        props["supertraits"] = list(rt.supertraits)
    yield GraphNode(e.kind, {"id": e.type_id}, props)
    yield GraphEdge("HAS_TYPE", e.scope_label, {"id": e.scope_id}, e.kind, {"id": e.type_id})
    yield GraphEdge("DEFINED_IN", e.kind, {"id": e.type_id}, "File", {"id": e.file_id})
    # File→entity provenance (ADR 0005): the per-file ownership handle the delta-sync engine scopes
    # by (File.id -> node), uniform across ingestors (Go/C's File-[:DEFINES]->x). Additive to DEFINED_IN.
    yield GraphEdge("DEFINES", "File", {"id": e.file_id}, e.kind, {"id": e.type_id})

    if e.kind == "Struct":
        for f in rt.fields:
            fid = ids.field_id(e.type_id, f.name)
            yield GraphNode("Field", {"id": fid}, {
                "name": f.name, "type": f.type, "visibility": f.visibility, "position": f.position,
                "line_start": f.line_start, "line_end": f.line_end,
            })
            yield GraphEdge("HAS_FIELD", "Struct", {"id": e.type_id}, "Field", {"id": fid})
    elif e.kind == "Enum":
        for v in rt.variants:
            vid = ids.variant_id(e.type_id, v.name)
            yield GraphNode("EnumVariant", {"id": vid}, {"name": v.name, "kind": v.kind})
            yield GraphEdge("HAS_VARIANT", "Enum", {"id": e.type_id}, "EnumVariant", {"id": vid})


def _emit_callable_node(cal: _Callable, linkage_key: str | None, unresolved_calls: int = 0) -> Iterator[GraphNode | GraphEdge]:
    fn = cal.raw
    props = {
        "name": fn.name,
        "visibility": fn.visibility,
        "modifiers": list(fn.modifiers),
        "is_async": "async" in fn.modifiers,
        "is_unsafe": "unsafe" in fn.modifiers,
        "return_type": fn.return_type,
        "source": fn.source,
        "doc": fn.doc,
        "line_start": fn.line_start,
        "line_end": fn.line_end,
        # Call sites the name pass couldn't resolve (an unknown-receiver/ambiguous call, or a call
        # inside a macro): > 0 means this callable's CALLES (and the callers of its unknown targets)
        # may be incomplete. A future deep rust-analyzer pass would resolve these and set it to 0 (#792).
        "unresolved_calls": unresolved_calls,
    }
    if linkage_key is not None:  # ADR 0004: a `pub` callable is externally visible -> cross-repo key
        props["linkage_key"] = linkage_key
    if cal.label == "Method":
        props.update(is_associated=fn.is_associated, self_kind=fn.self_kind, has_body=fn.has_body)
        yield GraphNode("Method", {"id": cal.id}, props)
        yield GraphEdge("HAS_METHOD", cal.owner_label, {"id": cal.owner_id}, "Method", {"id": cal.id})
    else:
        yield GraphNode("Function", {"id": cal.id}, props)
        yield GraphEdge("HAS_FUNCTION", _scope_label_of(cal.scope_id), {"id": cal.scope_id}, "Function", {"id": cal.id})
    # File→callable provenance (ADR 0005): the impl block / trait / free-fn's own file owns the edge.
    yield GraphEdge("DEFINES", "File", {"id": cal.file_id}, cal.label, {"id": cal.id})
    for i, p in enumerate(fn.parameters):
        pid = ids.parameter_id(cal.id, i, p.name)
        yield GraphNode("Parameter", {"id": pid}, {"name": p.name, "type": p.type, "position": p.position})
        yield GraphEdge("HAS_PARAMETER", cal.label, {"id": cal.id}, "Parameter", {"id": pid})


def _emit_const(scope_id: str, scope_label: str, file_id: str, rc: RawConst) -> Iterator[GraphNode | GraphEdge]:
    cid = ids.const_id(scope_id, rc.name)
    yield GraphNode("Const", {"id": cid}, {
        "name": rc.name, "kind": rc.kind, "type": rc.type, "visibility": rc.visibility,
        "source": rc.source, "line_start": rc.line_start, "line_end": rc.line_end,
    })
    yield GraphEdge("HAS_CONST", scope_label, {"id": scope_id}, "Const", {"id": cid})
    yield GraphEdge("DEFINES", "File", {"id": file_id}, "Const", {"id": cid})  # provenance (ADR 0005)


def _scope_label_of(scope_id: str) -> str:
    # module ids contain "/module/"; a crate-root item's scope id is the bare crate id ("/crate/").
    if "/module/" in scope_id:
        return "Module"
    if "/crate/" in scope_id:
        return "Crate"
    return "Project"


# -- pass 2: calls + type references -------------------------------------------------------

def _resolve_target(name: str | None, type_index: dict[str, list[_TypeEntry]]) -> _TypeEntry | None:
    if not name:
        return None
    matches = type_index.get(name)
    if not matches:
        return None
    return sorted(matches, key=lambda m: m.type_id)[0]  # deterministic on ambiguity


def _method_on_type(type_id: str, name: str, method_index) -> str | None:
    matches = [c for c in method_index.get(name, []) if c[0] == type_id]
    return sorted(matches, key=lambda c: c[1])[0][1] if matches else None


def _referenced_names(type_text: str | None) -> list[str]:
    if not type_text:
        return []
    return [n for n in _IDENTIFIER_RE.findall(type_text) if n not in _NON_TYPE_TOKENS]


def _real_linkage(cal: _Callable, crate_name: str, dotted_by_id, type_by_id) -> str | None:
    """ADR 0004 cross-repo `linkage_key` for a real callable, or None for a non-`pub` one (only a
    `pub` item is visible outside the crate, so only it can be a cross-repo target). Name-based, in
    the form a consumer writes: a free function -> `crate::module::name`; a method -> `Type::name`
    (a caller usually has the type in scope and writes `x.name()` / `Type::name()`, without the
    crate/module prefix)."""
    fn = cal.raw
    if fn.visibility != "pub":
        return None
    if cal.label == "Method":
        owner = type_by_id.get(cal.self_type_id)
        if owner is None:
            return None
        return ids.extern_function_key(f"{owner.name}::{fn.name}")
    dotted = dotted_by_id.get(cal.scope_id)  # None/"" for a crate-root free function
    parts = [crate_name, *(dotted.split(".") if dotted else []), fn.name]
    return ids.extern_function_key("::".join(parts))


def _path_segments(call) -> list[str]:
    raw = call.path or call.qualifier or ""
    return [s for s in raw.split("::") if s]


def _external_path(call, type_index, project_modules) -> "_External | None":
    """An unresolved `path` call (`a::b::func()`, `Type::assoc()`) -> an `_External` placeholder,
    UNLESS the leading segment names a project item (a project module or type) or is
    crate-relative (`crate`/`self`/`super`/`Self`) -- those are intra-project dangling refs, left
    dropped so a resync doesn't leave a stale node. stdlib roots -> origin="stdlib"."""
    segs = _path_segments(call)
    if not segs:
        return None
    first = segs[0]
    if first in _PROJECT_PATH_ROOTS or first in type_index or first in project_modules:
        return None
    qualified = "::".join([*segs, call.name])
    origin = "stdlib" if first in _STDLIB_ROOTS else "external"
    return _External(ids.extern_function_key(qualified), "Function", call.name, qualified, origin)


def _resolve_call(call, cal: _Callable, member_types: dict[str, str], type_index, function_index,
                  method_index, project_modules):
    """A resolved local target `(target_id, target_label, extra_props)`, an `_External` placeholder
    for a nameable out-of-project callee (ADR 0004), or None to drop (unnameable, or an
    intra-project dangling reference a resync should clean up rather than materialize)."""
    if call.kind == "free":
        cands = function_index.get(call.name)
        if not cands:
            return None  # a bare `foo()` -- no module/crate to name it by -> drop
        same = [c for c in cands if c[1] == cal.scope_id]
        return sorted(same or cands)[0][0], "Function", {}

    if call.kind == "path":
        q = call.qualifier
        if q in ("Self", "self", None) and cal.self_type_id:
            mid = _method_on_type(cal.self_type_id, call.name, method_index)
            if mid:
                return mid, "Method", {}
        elif q:
            tgt = _resolve_target(q, type_index)
            if tgt is not None:
                mid = _method_on_type(tgt.type_id, call.name, method_index)
                if mid:
                    return mid, "Method", {"receiver_type": tgt.name}
        cands = function_index.get(call.name)  # a `module::func()` free-function call
        if cands:
            return sorted(cands)[0][0], "Function", {}
        return _external_path(call, type_index, project_modules)  # `std::mem::swap`, `Type::new`, ...

    # method call: receiver.name()
    recv = call.receiver
    if recv == "self":
        if cal.self_type_id:
            mid = _method_on_type(cal.self_type_id, call.name, method_index)
            if mid:
                return mid, "Method", {}
        return None  # a missing method on our own type -> intra-project dangling -> drop
    if recv is not None:
        rtype = member_types.get(recv)
        if rtype is not None:
            names = _referenced_names(rtype)
            for tn in names:
                tgt = _resolve_target(tn, type_index)
                if tgt is not None:
                    mid = _method_on_type(tgt.type_id, call.name, method_index)
                    if mid:
                        return mid, "Method", {"receiver_type": tgt.name}
            # Receiver's type is known but no project type owns the method. If its head (outermost)
            # type is a project type, this is an intra-project dangling call -> drop; otherwise the
            # receiver is an external type (`String`, `Vec`, another crate's) -> a nameable method.
            head = names[0] if names else None
            if head is None or head in type_index:
                return None
            qualified = f"{head}::{call.name}"
            return _External(ids.extern_function_key(qualified), "Method", call.name, qualified,
                             "external", {"receiver_type": head})
    cands = method_index.get(call.name)  # unknown receiver -> deterministic name-based pick
    if cands:
        pick = sorted(cands, key=lambda x: x[1])[0][1]
        # >1 project method shares this name and the receiver type is unknown -> the pick is a guess.
        # Keep the best-effort edge but mark it `_approx` so _resolve_calls counts it as unresolved
        # (the concrete callee is uncertain). A `_approx` key is internal -- stripped before the edge.
        return pick, "Method", ({"_approx": True} if len(cands) > 1 else {})
    return None  # unknown receiver, no project method by that name -> can't name it -> drop


def _is_incomplete_dispatch(call, member_types: dict[str, str], generics: set[str], in_trait: bool = False) -> bool:
    """True when a call's edge (if any) can't name the CONCRETE callee -- so `who_calls` on the real
    target still misses this caller (#792):
      * a method on a GENERIC-param / `dyn Trait` / `impl Trait` receiver (keyed by the type variable or
        trait, not the monomorphized/vtable target -- even when it resolves to a project trait's decl);
      * a path call qualified by a generic param (`T::default()` -> keyed by `T`, not the concrete impl);
      * a `self.m()` call inside a TRAIT default method (`self` is the unknown implementor -- it binds to
        the trait's decl, not the overriding impl).
    A concrete receiver type or a concrete path (`std::mem::swap`, `String::new`), and `self.m()` in a
    concrete `impl`, are genuinely resolvable, so they are NOT flagged (counting them would be noise)."""
    if call.kind == "path":
        return call.qualifier in generics  # `T::default()` where T is a generic in scope
    if call.kind != "method":
        return False
    if call.receiver == "self":
        return in_trait  # trait default method: the concrete implementor is unknown; concrete impl is fine
    rt = member_types.get(call.receiver or "", "")
    if "dyn " in rt or "impl " in rt:
        return True
    base = re.sub(r"^'\w+\s+", "", rt.lstrip("&").strip())  # drop a reference lifetime: `&'a T` -> `T`
    base = base.replace("mut ", "").strip().split("<", 1)[0].strip()
    return base in generics


def _resolve_calls(cal: _Callable, type_fields, type_index, function_index, method_index,
                   project_modules, emitted_externs: set[str], type_by_id) -> tuple[list[GraphNode | GraphEdge], int]:
    """Resolve a callable's call sites ONCE -> (CALLS edges + extern placeholder nodes, unresolved
    count). A call site is counted as unresolved -- meaning this callable's callees (and the callers of
    its unknown targets) may be incomplete -- when:
      * it produced no edge (unknown receiver / intra-project dangling);
      * it's a macro-internal call-like site (`cal.raw.unresolved_macro_calls`);
      * it's a generic / `dyn` / `impl` dispatch (method on such a receiver, or a `T::assoc()` path):
        the edge, if any, targets a type-variable placeholder or a trait decl -- NOT the concrete
        method -- so this counts EVEN when it resolved to a project node (_is_incomplete_dispatch);
      * it's an ambiguous unknown-receiver name pick (`_approx`, >1 project method by that name).
    A plain stdlib/other-crate external (concrete receiver type, or a concrete path) is genuinely
    external and reconciles cross-repo, so it does NOT count. Generics in scope = the method's own +
    its enclosing impl block's + its owning type's (so `impl<T> Foo<T>` method params counted too)."""
    member_types: dict[str, str] = {}
    if cal.self_type_id:
        member_types.update(type_fields.get(cal.self_type_id, {}))
    member_types.update({p.name: p.type for p in cal.raw.parameters if p.type})
    generics = set(cal.raw.type_parameters) | set(cal.impl_generics)  # fn + enclosing-impl generics
    if cal.self_type_id:  # + the owning type's own generics (`struct Foo<T>` / `trait Bar<T>`)
        owner = type_by_id.get(cal.self_type_id)
        if owner is not None:
            generics |= set(getattr(owner.raw, "type_parameters", ()))
    in_trait = cal.owner_label == "Trait"  # a trait default method: self dispatches to an unknown impl

    items: list[GraphNode | GraphEdge] = []
    emitted: set[str] = set()
    unresolved = cal.raw.unresolved_macro_calls  # macro-internal calls never produce an edge
    for call in cal.raw.calls:
        hit = _resolve_call(call, cal, member_types, type_index, function_index, method_index, project_modules)
        approx = isinstance(hit, tuple) and hit[2].pop("_approx", False)  # strip the internal marker
        if hit is None:
            unresolved += 1  # a nameable-but-unresolved call site -> no edge, flag incompleteness
            continue
        # An edge IS emitted below, but count the site as unresolved when it can't name the concrete
        # callee -- a generic/dyn/impl dispatch (even one resolved to a project trait decl, whose real
        # impls stay unknown) or an ambiguous unknown-receiver name pick. Per call site, before dedup.
        if approx or _is_incomplete_dispatch(call, member_types, generics, in_trait):
            unresolved += 1
        if isinstance(hit, _External):
            # Unresolved but nameable (stdlib / other-crate / external type): a placeholder node
            # keyed by its cross-repo linkage, reached by an ordinary CALLS edge (ADR 0004). A
            # later ingest of the defining repo heals the edge onto the real node (core reconcile).
            if hit.eid in emitted:
                continue
            emitted.add(hit.eid)
            if hit.eid not in emitted_externs:
                emitted_externs.add(hit.eid)
                items.append(GraphNode(hit.label, {"id": hit.eid}, {
                    "name": hit.name, "qualified_name": hit.qualified, "resolved": False,
                    "origin": hit.origin, "linkage_key": hit.eid,
                }))
            items.append(GraphEdge("CALLS", cal.label, {"id": cal.id}, hit.label, {"id": hit.eid},
                                   {"resolution": "external", **hit.extra}))
            continue
        target_id, target_label, extra = hit
        if target_id == cal.id or target_id in emitted:
            continue
        emitted.add(target_id)
        items.append(GraphEdge("CALLS", cal.label, {"id": cal.id}, target_label, {"id": target_id},
                               {"resolution": "name", **extra}))
    return items, unresolved


def _depends_on(source_label: str, source_id: str, type_texts, type_index, skip_type_id: str | None) -> Iterator[GraphEdge]:
    names: set[str] = set()
    for tt in type_texts:
        names.update(_referenced_names(tt))
    emitted: set[str] = set()
    for name in names:
        target = _resolve_target(name, type_index)
        if target is None or target.type_id in emitted or target.type_id == skip_type_id:
            continue
        emitted.add(target.type_id)
        yield GraphEdge("DEPENDS_ON", source_label, {"id": source_id}, target.kind, {"id": target.type_id})


# -- helpers -------------------------------------------------------------------------------

def _norm(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def _basename(path: str) -> str:
    return _norm(path).rsplit("/", 1)[-1]


def _package_name(package_dir: str, project_id: str) -> str:
    """Path-derived display name for a Package node: the manifest directory's basename (Cargo's own
    hyphen->underscore normalization), or the project name for the repo-root package. Id-derived (the
    package id embeds the dir), so every member file emits the identical node. git_sync's reconcile
    overrides it with the declared `[package].name` when a manifest gives a different one."""
    if not package_dir:
        return project_id.split("://", 1)[-1]
    return package_dir.rsplit("/", 1)[-1].replace("-", "_")
