"""Project-wide pass (phase 2): turn the per-file RawFile list into graph nodes/edges, applying
the cross-file decisions a single file can't -- materializing the flat package tier, attaching
methods to their receiver types, resolving EMBEDS / DEPENDS_ON type references, and best-effort
CALLS resolution (same-package free funcs, receiver/param/field-typed methods, and
package-qualified calls into another project package).

Name-based only -- this is the fast tree-sitter engine, so every CALLS edge is stamped
`resolution="name"`. Ambiguous or unresolved references produce NO edge rather than a wrong
guess (the same trade-off as the Java/C#/PL/SQL ingestors).

**IMPLEMENTS is deliberately not emitted here.** Deciding that a struct satisfies an interface
means comparing full method sets with resolved types -- Go's satisfaction is structural/implicit
-- which is deep-pass work (a go/types or gopls engine), noted in plugin.py. Guessing it from
name overlap alone would be wrong, so the fast pass stays silent on it.

Go resolution specifics:
- **Bare references are same-package only.** In Go you cannot name another package's type/func
  unqualified, so a bare `Store` resolves solely within the referring file's package (never a
  cross-package guess). Cross-package references are always qualified (`model.Store`,
  `model.New()`) and resolved via the file's import set -> the target project package.
- **Methods attach to their receiver type** (HAS_METHOD): `func (s *Store) Put()` -> Method
  "Put" on Struct "Store". The receiver variable (`s`) types like Java's `this`; param and
  receiver-struct-field names type the same way for call resolution."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field

from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.ingestors.go import gomod, ids
from auryn.ingestors.go.raw_model import RawFile, RawFunction, RawType

# ADR 0004: a best-effort Go standard-library root set. An unresolved call into an imported
# package whose import path's FIRST segment is one of these is surfaced with origin="stdlib" (a
# terminal external that never resolves to an ingested repo), vs origin="external" (a candidate a
# sibling repo could define). Matched on the first path segment, so `net/http` -> `net`,
# `encoding/json` -> `encoding`. Not exhaustive -- covers the common surface that matters here.
_GO_STDLIB = frozenset({
    "fmt", "os", "io", "strings", "strconv", "errors", "bytes", "bufio",
    "sync", "time", "context", "net", "http", "encoding", "json", "sort", "math",
})


def _go_origin(import_path: str) -> str:
    return "stdlib" if import_path.split("/", 1)[0] in _GO_STDLIB else "external"


@dataclass
class _Type:
    type_id: str
    kind: str  # "Struct" | "Interface" | "TypeAlias"
    name: str
    pkg_dir: str       # repo-relative package directory -- the node-id key
    pkg_name: str      # the `package` clause name -- display only (qualified_name)
    import_path: str   # canonical import path (module path + dir) -- resolution match key + linkage
    raw: RawType
    file_id: str
    import_paths: dict = field(default_factory=dict)  # the defining file's alias->import path, so a
    #   qualified embed/field type (`util.Base`) resolves via import context even when two packages
    #   share a `package` clause name (empty for a seed stub, which is never re-emitted).


@dataclass
class _Func:
    func_id: str
    name: str
    pkg_dir: str       # repo-relative package directory -- the node-id key
    pkg_name: str      # the `package` clause name -- display only
    import_path: str   # canonical import path -- resolution match key + cross-repo linkage
    raw: RawFunction
    imports: dict  # alias -> package name (last path segment), legacy display proxy
    import_paths: dict = field(default_factory=dict)  # alias -> full WRITTEN import path (resolution + ADR 0004)
    owner: _Type | None = None  # set for methods
    label: str = "Function"  # "Function" | "Method"
    param_types: list = field(default_factory=list)
    file_id: str = ""  # the File that DEFINES this func/method (per-file provenance for delta-sync)


@dataclass
class GoGraphIndex:
    """Go resolution tables seeded from the GRAPH for the NON-emit files (ADR 0005), so
    `resolve_incremental` can resolve an emit file's cross-package refs/calls without re-parsing
    the project. Types are lightweight `_Type` stubs (raw=None) -- only id/kind/name/package are
    needed to point an edge at a non-emit target. Reconstructed by `go.incremental.read_go_index`."""

    project_import_paths: set = field(default_factory=set)  # every project package's import path
    type_by_import_path: dict = field(default_factory=dict)  # (import_path, name) -> _Type stub
    func_by_name: dict = field(default_factory=dict)      # name -> [(func_id, import_path)]
    method_by_name: dict = field(default_factory=dict)    # name -> [(owner_type_id, method_id)]
    module_map: dict = field(default_factory=dict)        # module_dir -> module_path (from go.mod nodes)


def resolve(raw_files: list[RawFile], project_id: str, module_map: dict | None = None) -> Iterator[GraphNode | GraphEdge]:
    yield from _resolve(raw_files, project_id, seed=None, module_map=module_map or {})


def resolve_incremental(emit_raws: list[RawFile], project_id: str, index: GoGraphIndex) -> list:
    """Emit graph items for ONLY `emit_raws`, resolving cross-package refs/calls against `index`
    (graph-seeded non-emit symbols) merged with the emit files' own. The blast-radius analogue of
    resolve() (ADR 0005): same emitters, indices seeded rather than whole-project. The module map
    comes from the graph (`index.module_map`), kept current by git_sync's go.mod-change full fallback."""
    return list(_resolve(emit_raws, project_id, seed=index, module_map=index.module_map))


def make_type_stub(type_id: str, kind: str, name: str, pkg_dir: str, pkg_name: str,
                   import_path: str, fields: list) -> _Type:
    """A lightweight `_Type` for a NON-emit type the seed provides: id/kind/name + its package's
    dir/name/import_path (to point an edge + match a cross-package ref), plus `fields` (name/type)
    so an emit method whose receiver is this type can still resolve field-typed calls."""
    raw = RawType(kind=kind, name=name, fields=fields)
    return _Type(type_id, kind, name, pkg_dir, pkg_name, import_path, raw, file_id="")


def callable_locations(raw_files: list[RawFile], project_id: str):
    """Every Go callable's SCIP-join location (graph id, file, 1-based line span, name, called names) --
    the bridge the deep (scip-go / SCIP) pass needs. Reuses the SAME id builders as the fast pass
    (dir-keyed package -> type/func/method ids), so deep CALLS edges land byte-identically on the nodes
    resolve() authored -- no module map or type inference needed: ids are dir-keyed, and a Go method's
    receiver type is always in the method's own package, so a per-(package-key, type-name) index resolves
    the owner type exactly as _resolve's (import_path, name) index does. A parity test asserts these ids
    are a subset of resolve()'s Function/Method node ids. The core.scip_resolve import is deferred here
    (deep-only) so the fast pass never requires it (buildable against an older auryn-core).

    Interface method SPECS are deliberately excluded: scip-go encodes them as *term* descriptors
    (`Reader#Read.`, not `Read().`), so they never match the callable-symbol gate and a call through an
    interface-typed receiver resolves (in scip-go) to the interface term rather than a concrete method --
    interface-dispatch deep resolution is a follow-up, so including these locations would be dead weight."""
    from auryn.core.scip_resolve import CallableLocation

    raw_files = sorted(raw_files, key=lambda rf: _norm(rf.path))
    pid_of_path: dict[str, str] = {}
    pkey_of_path: dict[str, str] = {}
    type_id_by_pkey_name: dict[tuple[str, str], str] = {}
    for rf in raw_files:
        if not rf.package:
            continue
        pkey = _pkg_key(_dir_of(rf.path), rf.package)
        pid = ids.package_id(project_id, pkey)
        pid_of_path[rf.path] = pid
        pkey_of_path[rf.path] = pkey
        for rt in rf.types:  # first-wins (setdefault), matching _resolve's type_by_import_path
            type_id_by_pkey_name.setdefault((pkey, rt.name), ids.type_id(pid, rt.kind, rt.name, rt.type_parameters))

    out = []
    for rf in raw_files:
        if not rf.package:
            continue
        pid, pkey, rel = pid_of_path[rf.path], pkey_of_path[rf.path], _norm(rf.path)
        for rfn in rf.functions:
            ptypes = [p.type for p in rfn.parameters]
            if rfn.receiver is None:
                cid, label = ids.function_id(pid, rfn.name, ptypes), "Function"
            else:
                owner_tid = type_id_by_pkey_name.get((pkey, rfn.receiver.type_name))
                if owner_tid is None:  # receiver type not a project type (shouldn't happen in valid Go)
                    continue
                cid, label = ids.method_id(owner_tid, rfn.name, ptypes), "Method"
            out.append(CallableLocation(cid, label, rel, rfn.line_start, rfn.line_end, rfn.name,
                                        frozenset(c.name for c in rfn.calls)))
    return out


def _resolve(raw_files: list[RawFile], project_id: str, seed: "GoGraphIndex | None",
             module_map: dict) -> Iterator[GraphNode | GraphEdge]:
    # Stable path order so a symbol DEFINED in several files (multi-program dirs: several
    # `package main`/`func main`) reduces to an order-INDEPENDENT winner -- identical here and in
    # resolve_incremental's subset, the precondition for incremental == full (ADR 0005).
    raw_files = sorted(raw_files, key=lambda rf: _norm(rf.path))
    module_dirs = set(module_map.keys())

    # -- files + module/package tier (issue "Go package collision") -------------------------
    # A package is keyed by its DIRECTORY (pure path), not the `package` clause name, so two dirs
    # sharing a name stay distinct. Its canonical IMPORT PATH (module path + dir) is carried as a
    # property and is the match key for cross-package refs AND the cross-repo linkage key.
    file_ids: dict[str, str] = {}
    pkg_key_of: dict[str, str] = {}          # rf.path -> package key (dir, or dir#test, see _pkg_key)
    import_path_of: dict[str, str] = {}      # package key -> import path
    pkg_name_of: dict[str, str] = {}         # package key -> `package` clause name (display)
    dir_of_key: dict[str, str] = {}          # package key -> directory (for module ownership)
    pkg_defs: set[tuple[str, str]] = set()   # (file_id, package_id): File-DEFINES-Package provenance
    mod_defs: set[tuple[str, str]] = set()   # (file_id, module_id): File-DEFINES-Module provenance
    for rf in raw_files:
        fid = ids.file_id(project_id, rf.path)
        file_ids[rf.path] = fid
        yield GraphNode("File", {"id": fid}, {"name": _basename(rf.path), "path": _norm(rf.path)})
        yield GraphEdge("HAS_FILE", "Project", {"id": project_id}, "File", {"id": fid})
        if not rf.package:
            continue
        pdir = _dir_of(rf.path)
        # Go's EXTERNAL test package (`package foo_test`) legally shares a directory with `package
        # foo`; `_pkg_key` gives it a distinct key (and the import path a `#test` marker) so the two
        # compilation units -- and their same-named symbols -- never merge.
        pkey = _pkg_key(pdir, rf.package)
        ip = gomod.import_path_for_dir(pdir, module_map)
        if not ip:  # a repo-ROOT package with no go.mod -> the clause name is the best import proxy
            ip = rf.package  # (matches what a GOPATH-style `import "<name>"` consumer writes)
        if rf.package.endswith("_test"):
            ip = f"{ip}#test"
        pkg_key_of[rf.path] = pkey
        import_path_of[pkey] = ip
        pkg_name_of.setdefault(pkey, rf.package)
        dir_of_key[pkey] = pdir
        pkg_defs.add((fid, ids.package_id(project_id, pkey)))
        owner = gomod.owning_module_dir(pdir, module_dirs)
        if owner is not None:
            mod_defs.add((fid, ids.module_id(project_id, owner)))

    # Module tier (one node per go.mod). Here we author the node + the Project->Module backbone with
    # id-derived props (module path from the map); git_sync's reconcile adds the go version + the
    # require DEPENDS_ON. File-DEFINES->Module is the GC handle (aggregation refcount), like Package.
    for mdir in sorted(module_dirs):
        mid = ids.module_id(project_id, mdir)
        yield GraphNode("Module", {"id": mid}, {
            "name": module_map[mdir], "qualified_name": module_map[mdir], "dir": mdir,
        })
        yield GraphEdge("HAS_MODULE", "Project", {"id": project_id}, "Module", {"id": mid})

    # Package nodes (dir-keyed) under their owning Module (or the Project when no go.mod covers them).
    for pkey in sorted(import_path_of):
        pid = ids.package_id(project_id, pkey)
        yield GraphNode("Package", {"id": pid}, {
            "name": pkg_name_of[pkey], "import_path": import_path_of[pkey],
            "qualified_name": import_path_of[pkey],
        })
        owner = gomod.owning_module_dir(dir_of_key[pkey], module_dirs)
        if owner is not None:
            yield GraphEdge("HAS_PACKAGE", "Module", {"id": ids.module_id(project_id, owner)}, "Package", {"id": pid})
        else:
            yield GraphEdge("HAS_PACKAGE", "Project", {"id": project_id}, "Package", {"id": pid})
    # File->Package / File->Module provenance (ADR 0005): the GC handle so an aggregation node whose
    # last member file is removed is reclaimed exactly as a full recompute would. One edge per
    # declaring file (co-contributor closure); props are id-derived, so both are excluded from the
    # co-DEFINER winner closure (GO_SPEC.aggregation_labels) -- editing one file must not pull siblings.
    for fid, pid in sorted(pkg_defs):
        yield GraphEdge("DEFINES", "File", {"id": fid}, "Package", {"id": pid})
    for fid, mid in sorted(mod_defs):
        yield GraphEdge("DEFINES", "File", {"id": fid}, "Module", {"id": mid})

    # -- index types (needed before methods can find their receiver owner) ------------------
    types: list[_Type] = []
    for rf in raw_files:
        if not rf.package:
            continue
        pkey = pkg_key_of[rf.path]
        ip = import_path_of[pkey]
        pid = ids.package_id(project_id, pkey)
        file_imports = _import_paths(rf)  # the defining file's alias->import-path, for embed/field refs
        for rt in rf.types:
            tid = ids.type_id(pid, rt.kind, rt.name, rt.type_parameters)
            types.append(_Type(tid, rt.kind, rt.name, pkey, rf.package, ip, rt, file_ids[rf.path], file_imports))

    type_by_import_path: dict[tuple[str, str], _Type] = {}
    type_by_name: dict[str, list[_Type]] = defaultdict(list)
    for t in types:
        type_by_import_path.setdefault((t.import_path, t.name), t)
        type_by_name[t.name].append(t)

    # Project import-path set + cross-package type index seeded from the graph (non-emit files), so an
    # emit file's refs into an untouched package still resolve. Emit types win (setdefault).
    project_import_paths = set(import_path_of.values())
    if seed is not None:
        project_import_paths |= seed.project_import_paths
        for k, v in seed.type_by_import_path.items():
            if k not in type_by_import_path:
                type_by_import_path[k] = v
                type_by_name[v.name].append(v)

    # -- index functions/methods ------------------------------------------------------------
    funcs: list[_Func] = []
    for rf in raw_files:
        if not rf.package:
            continue
        pkey = pkg_key_of[rf.path]
        ip = import_path_of[pkey]
        pid = ids.package_id(project_id, pkey)
        imports = _import_map(rf)
        import_paths = _import_paths(rf)
        for rfn in rf.functions:
            ptypes = [p.type for p in rfn.parameters]
            if rfn.receiver is None:
                fid = ids.function_id(pid, rfn.name, ptypes)
                funcs.append(_Func(fid, rfn.name, pkey, rf.package, ip, rfn, imports, import_paths,
                                   label="Function", param_types=ptypes, file_id=file_ids[rf.path]))
            else:
                owner = type_by_import_path.get((ip, rfn.receiver.type_name))
                if owner is None:  # receiver type not a project type (shouldn't happen in valid Go)
                    continue
                mid = ids.method_id(owner.type_id, rfn.name, ptypes)
                funcs.append(_Func(mid, rfn.name, pkey, rf.package, ip, rfn, imports, import_paths,
                                   owner=owner, label="Method", param_types=ptypes, file_id=file_ids[rf.path]))

    func_by_name: dict[str, list[tuple[str, str]]] = defaultdict(list)  # name -> [(func_id, import_path)]
    method_by_name: dict[str, list[tuple[str, str]]] = defaultdict(list)  # name -> [(owner_id, method_id)]
    for f in funcs:
        if f.label == "Function":
            func_by_name[f.name].append((f.func_id, f.import_path))
        else:
            method_by_name[f.name].append((f.owner.type_id, f.func_id))
    # Interface method specs are also call targets (a call through an interface-typed receiver).
    for t in types:
        if t.kind == "Interface" and t.raw is not None:
            for m in t.raw.methods:
                mid = ids.method_id(t.type_id, m.name, [p.type for p in m.parameters])
                method_by_name[m.name].append((t.type_id, mid))
    # Seed non-emit funcs/methods (the seed excludes emit files, so no duplication).
    if seed is not None:
        for name, entries in seed.func_by_name.items():
            func_by_name[name].extend(entries)
        for name, entries in seed.method_by_name.items():
            method_by_name[name].extend(entries)

    # -- pass 1: nodes + structural edges (landing nodes for pass 2) ------------------------
    # Dedup NODES by id (keep first; files are sorted, so the winner is order-INDEPENDENT). A symbol
    # DEFINED in several files (multi-program dirs: two `func main` in package main) thus yields ONE
    # node with deterministic props -- identical in full and incremental, regardless of the write
    # path's MERGE order. Every definer's EDGES still flow (a DEFINES from each file; CALLS unioned
    # in pass 2), matching a full recompute.
    seen_nodes: set = set()

    def _once(it):
        if isinstance(it, GraphNode):
            k = (it.label, str(it.key["id"]))
            if k in seen_nodes:
                return False
            seen_nodes.add(k)
        return True

    for t in types:
        for it in _emit_type(t, project_id):
            if _once(it):
                yield it
    for f in funcs:
        for it in _emit_func(f, project_id):
            if _once(it):
                yield it
    for rf in raw_files:
        if not rf.package:
            continue
        pid = ids.package_id(project_id, pkg_key_of[rf.path])
        for c in rf.consts:
            cid = ids.const_id(pid, c.name)
            node = GraphNode("Const", {"id": cid}, {
                "name": c.name, "value": c.value, "is_exported": _exported(c.name),
                "line_start": c.line_start, "line_end": c.line_end,
            })
            if _once(node):
                yield node
            yield GraphEdge("HAS_CONST", "Package", {"id": pid}, "Const", {"id": cid})
            yield GraphEdge("DEFINES", "File", {"id": file_ids[rf.path]}, "Const", {"id": cid})  # provenance

    # -- pass 2: EMBEDS, DEPENDS_ON, CALLS (over the EMIT types/funcs only) ------------------
    # A qualifier (clause name or import-path last segment) -> import path, when UNIQUE -- the
    # fallback for a field/embed type ref that has no import-block context (an ambiguous name resolves
    # to nothing, per the golden rule). A call site WITH import context resolves via its written paths.
    qualifier_ips: dict[str, set] = defaultdict(set)
    for pdir, ip in import_path_of.items():
        qualifier_ips[pkg_name_of[pdir]].add(ip)
        qualifier_ips[ip.rsplit("/", 1)[-1]].add(ip)
    if seed is not None:
        for (ip, _n), t in seed.type_by_import_path.items():
            qualifier_ips[t.pkg_name].add(ip)
            qualifier_ips[ip.rsplit("/", 1)[-1]].add(ip)
    qualifier_to_ip = {q: next(iter(s)) for q, s in qualifier_ips.items() if len(s) == 1}

    resolver = _Resolver(project_import_paths, type_by_import_path, type_by_name,
                         func_by_name, method_by_name, qualifier_to_ip)
    for t in types:
        if t.raw is None:
            continue  # a seed stub (non-emit type) -- not re-emitted, only a resolution target
        yield from _emit_embeds(t, resolver)
        yield from _emit_field_depends(t, resolver)
    for f in funcs:
        yield from _emit_func_depends(f, resolver)
        for it in _emit_calls(f, resolver):  # extern placeholder nodes also dedup by id
            if _once(it):
                yield it


# -- resolver ------------------------------------------------------------------------------

class _Resolver:
    def __init__(self, project_import_paths, type_by_import_path, type_by_name,
                 func_by_name, method_by_name, qualifier_to_ip):
        self.project_import_paths = project_import_paths
        self.type_by_import_path = type_by_import_path
        self.type_by_name = type_by_name
        self.func_by_name = func_by_name
        self.method_by_name = method_by_name
        self.qualifier_to_ip = qualifier_to_ip  # unique clause-name/last-segment -> import path
        self.emitted_externs: set[str] = set()  # placeholder ids emitted this pass (dedup, ADR 0004)

    def ip_for_qualifier(self, qualifier: str, import_paths: dict) -> str | None:
        """Map a selector qualifier (`model` in `model.New`) to the project package's IMPORT PATH it
        names: first via the file's own import block (`import_paths[alias]` -- the path the source
        literally writes, which equals the target package's canonical import path), else a UNIQUE
        clause-name / last-segment match (for a field/embed ref with no import context). None when it
        names no project package (an external/stdlib package -> handled as a placeholder) or is
        ambiguous."""
        written = import_paths.get(qualifier)
        if written is not None:
            # An explicit import is AUTHORITATIVE: if the source imports a path that isn't a project
            # package, the qualifier names that external package (-> a placeholder), even if a local
            # package happens to share its last segment. Only fall back to the name heuristic when
            # there is NO import context (an embed/field ref carrying no import block).
            return written if written in self.project_import_paths else None
        return self.qualifier_to_ip.get(qualifier)

    def resolve_type_ref(self, ref: str, from_import_path: str, import_paths: dict) -> _Type | None:
        """A "Name" (same-package only) or "pkg.Name" (cross-package via the import block) ref -> a
        project type, or None. Bare names never resolve across packages (Go forbids unqualified
        cross-package references), so no wrong-package guess is possible."""
        if "." in ref:
            qualifier, name = ref.split(".", 1)
            ip = self.ip_for_qualifier(qualifier, import_paths)
            if ip is None:
                return None
            return self.type_by_import_path.get((ip, name))
        return self.type_by_import_path.get((from_import_path, ref))


# -- pass 1 emitters -----------------------------------------------------------------------

def _emit_type(t: _Type, project_id: str) -> Iterator[GraphNode | GraphEdge]:
    pid = ids.package_id(project_id, t.pkg_dir)
    yield GraphNode(t.kind, {"id": t.type_id}, _type_props(t))
    yield GraphEdge("HAS_TYPE", "Package", {"id": pid}, t.kind, {"id": t.type_id})
    yield GraphEdge("DEFINED_IN", t.kind, {"id": t.type_id}, "File", {"id": t.file_id})
    # File→entity provenance (ADR 0005): the per-file ownership handle the delta-sync engine scopes
    # by (File.id -> node), uniform across ingestors (C's File-[:DEFINES]->x). Additive to DEFINED_IN.
    yield GraphEdge("DEFINES", "File", {"id": t.file_id}, t.kind, {"id": t.type_id})

    for f in t.raw.fields:
        fid = ids.field_id(t.type_id, f.name)
        yield GraphNode("Field", {"id": fid}, {
            "name": f.name, "type": f.type, "is_exported": _exported(f.name),
            "line_start": f.line_start, "line_end": f.line_end,
        })
        yield GraphEdge("HAS_FIELD", t.kind, {"id": t.type_id}, "Field", {"id": fid})

    for m in t.raw.methods:  # interface method specs
        ptypes = [p.type for p in m.parameters]
        mid = ids.method_id(t.type_id, m.name, ptypes)
        mprops: dict = {
            "name": m.name, "kind": "INTERFACE_METHOD", "is_exported": _exported(m.name),
            "receiver": None, "return_type": m.return_type or None,
            "source": "", "doc": m.doc, "line_start": m.line_start, "line_end": m.line_end,
        }
        if _exported(m.name):  # cross-repo key (ADR 0004): `<import-path>.Type.Method`
            mprops["linkage_key"] = ids.extern_function_key(f"{t.import_path}.{t.name}.{m.name}")
        yield GraphNode("Method", {"id": mid}, mprops)
        yield GraphEdge("HAS_METHOD", t.kind, {"id": t.type_id}, "Method", {"id": mid})
        yield GraphEdge("DEFINES", "File", {"id": t.file_id}, "Method", {"id": mid})  # provenance
        for i, p in enumerate(m.parameters):
            yield from _emit_parameter(mid, p, i)


def _type_props(t: _Type) -> dict:
    props: dict = {
        "name": t.name,
        "qualified_name": f"{t.pkg_name}.{t.name}" if t.pkg_name else t.name,
        "is_exported": _exported(t.name),
        "type_parameters": list(t.raw.type_parameters),
        "source": t.raw.source,
        "doc": t.raw.doc,
        "line_start": t.raw.line_start,
        "line_end": t.raw.line_end,
    }
    if t.kind == "TypeAlias":
        props["underlying"] = t.raw.underlying
        props["is_alias"] = t.raw.is_alias
    return props


def _emit_func(f: _Func, project_id: str) -> Iterator[GraphNode | GraphEdge]:
    rfn = f.raw
    if f.label == "Function":
        pid = ids.package_id(project_id, f.pkg_dir)
        props: dict = {
            "name": rfn.name, "kind": "FUNCTION", "is_exported": _exported(rfn.name),
            "return_type": rfn.return_type or None, "source": rfn.source, "doc": rfn.doc,
            "line_start": rfn.line_start, "line_end": rfn.line_end,
        }
        if _exported(rfn.name):  # cross-repo key (ADR 0004): a consumer names it `<import-path>.Func`
            props["linkage_key"] = ids.extern_function_key(f"{f.import_path}.{rfn.name}")
        yield GraphNode("Function", {"id": f.func_id}, props)
        yield GraphEdge("HAS_FUNCTION", "Package", {"id": pid}, "Function", {"id": f.func_id})
    else:
        recv = rfn.receiver
        props = {
            "name": rfn.name, "kind": "METHOD", "is_exported": _exported(rfn.name),
            "receiver": recv.name or None, "receiver_type": recv.type_name,
            "is_pointer_receiver": recv.is_pointer, "return_type": rfn.return_type or None,
            "source": rfn.source, "doc": rfn.doc,
            "line_start": rfn.line_start, "line_end": rfn.line_end,
        }
        if _exported(rfn.name):  # cross-repo key (ADR 0004): `<import-path>.Type.Method`
            props["linkage_key"] = ids.extern_function_key(f"{f.owner.import_path}.{f.owner.name}.{rfn.name}")
        yield GraphNode("Method", {"id": f.func_id}, props)
        yield GraphEdge("HAS_METHOD", f.owner.kind, {"id": f.owner.type_id}, "Method", {"id": f.func_id})
    yield GraphEdge("DEFINES", "File", {"id": f.file_id}, f.label, {"id": f.func_id})  # provenance (ADR 0005)
    for i, p in enumerate(rfn.parameters):
        yield from _emit_parameter(f.func_id, p, i)


def _emit_parameter(owner_id: str, param, position: int) -> Iterator[GraphNode | GraphEdge]:
    pid = ids.parameter_id(owner_id, position, param.name)
    yield GraphNode("Parameter", {"id": pid}, {
        "name": param.name, "type": param.type, "position": param.position,
        "is_variadic": param.is_variadic,
    })
    yield GraphEdge("HAS_PARAMETER", _label_of(owner_id), {"id": owner_id}, "Parameter", {"id": pid})


def _label_of(owner_id: str) -> str:
    return "Function" if ".func." in owner_id else "Method"


# -- pass 2 emitters -----------------------------------------------------------------------

def _emit_embeds(t: _Type, r: _Resolver) -> Iterator[GraphEdge]:
    emitted: set[str] = set()
    for ref in t.raw.embeds:
        # A same-package embed resolves by the type's own import path; a qualified one (`model.Base`)
        # via the defining file's import block, so it still resolves when two packages share a clause
        # name (the importer's path is unambiguous) rather than needing a globally-unique name.
        target = r.resolve_type_ref(ref, t.import_path, t.import_paths)
        if target is None or target.type_id == t.type_id or target.type_id in emitted:
            continue
        if t.kind == "Interface" and target.kind != "Interface":
            continue  # an interface only embeds interfaces
        emitted.add(target.type_id)
        yield GraphEdge("EMBEDS", t.kind, {"id": t.type_id}, target.kind, {"id": target.type_id})


def _emit_field_depends(t: _Type, r: _Resolver) -> Iterator[GraphEdge]:
    for f in t.raw.fields:
        fid = ids.field_id(t.type_id, f.name)
        # Resolve a qualified field type (`util.Base`) via the defining file's import block, so it
        # resolves by the importer's unambiguous path even when two packages share a clause name.
        yield from _depends(
            "Field", fid, f.type_refs, t.import_path, t.import_paths, t.type_id, r
        )


def _emit_func_depends(f: _Func, r: _Resolver) -> Iterator[GraphEdge]:
    skip = f.owner.type_id if f.owner is not None else None
    yield from _depends(f.label, f.func_id, f.raw.type_refs, f.import_path, f.import_paths, skip, r)


def _depends(from_label, from_id, refs, from_import_path, import_paths, skip_type_id, r) -> Iterator[GraphEdge]:
    emitted: set[str] = set()
    for ref in refs:
        target = r.resolve_type_ref(ref, from_import_path, import_paths)
        if target is None or target.type_id == skip_type_id or target.type_id in emitted:
            continue
        emitted.add(target.type_id)
        yield GraphEdge("DEPENDS_ON", from_label, {"id": from_id}, target.kind, {"id": target.type_id})


def _emit_calls(f: _Func, r: _Resolver) -> Iterator[GraphNode | GraphEdge]:
    member_types = _member_types(f)
    emitted: set[str] = set()
    for call in f.raw.calls:
        hit = _resolve_call(call, f, member_types, r)
        if hit is not None:
            target_id, target_label = hit
            if target_id == f.func_id or target_id in emitted:
                continue
            emitted.add(target_id)
            yield GraphEdge("CALLS", f.label, {"id": f.func_id}, target_label, {"id": target_id},
                            {"resolution": "name"})
            continue
        # Unresolved -- but if we can NAME the callee as belonging to an imported non-project
        # package/type (a stdlib/third-party symbol, or a sibling repo not yet ingested), surface
        # it as a placeholder node reached by an ordinary CALLS edge (ADR 0004), so it heals to the
        # real definition when that repo lands in the same graph. Unnameable calls (bare builtins
        # like len/append, or an intra-project dangling symbol) stay dropped.
        ext = _external_call(call, f, member_types, r)
        if ext is None:
            continue
        eid, label, props = ext
        if eid in emitted:
            continue
        emitted.add(eid)
        if eid not in r.emitted_externs:
            r.emitted_externs.add(eid)
            yield GraphNode(label, {"id": eid}, props)
        yield GraphEdge("CALLS", f.label, {"id": f.func_id}, label, {"id": eid},
                        {"resolution": "external"})


def _member_types(f: _Func) -> dict:
    """identifier -> type-ref string, for names whose static type this file knows: the receiver
    (its owner type), the parameters, and (for a method) the receiver struct's fields."""
    out: dict = {}
    rfn = f.raw
    if rfn.receiver is not None and rfn.receiver.name:
        out[rfn.receiver.name] = rfn.receiver.type_name
    for p in rfn.parameters:
        if p.name:
            out[p.name] = p.type
    if f.owner is not None:
        for fld in f.owner.raw.fields:
            out.setdefault(fld.name, fld.type)
    return out


def _resolve_call(call, f: _Func, member_types: dict, r: _Resolver):
    """(target_id, target_label) or None. A bare `foo()` -> a same-package free function; a
    `recv.foo()` where recv is a receiver/param/field of a known type -> that type's method; a
    `pkg.Foo()` where pkg is a project package -> that package's free function."""
    if call.receiver is None:
        cands = sorted(fid for (fid, ip) in r.func_by_name.get(call.name, []) if ip == f.import_path)
        return (cands[0], "Function") if cands else None

    if call.receiver in member_types:
        target = r.resolve_type_ref(member_types[call.receiver], f.import_path, f.import_paths)
        if target is None:
            return None
        cands = sorted(mid for (owner_id, mid) in r.method_by_name.get(call.name, []) if owner_id == target.type_id)
        return (cands[0], "Method") if cands else None

    ip = r.ip_for_qualifier(call.receiver, f.import_paths)
    if ip is not None:
        cands = sorted(fid for (fid, p) in r.func_by_name.get(call.name, []) if p == ip)
        return (cands[0], "Function") if cands else None
    return None


def _external_call(call, f: _Func, member_types: dict, r: _Resolver):
    """An unresolved call we can still NAME as an external symbol -> (placeholder_id, label,
    props), else None (unnameable -> leave dropped). Two shapes qualify (ADR 0004):

    * `pkg.Func()` where `pkg` is an imported *non-project* package (`fmt.Println`, `errors.New`)
      -> a placeholder Function keyed `pkg.Func`.
    * `recv.Method()` where `recv`'s static type is an imported *non-project* `pkg.Type`
      (a param/field/receiver typed `io.Reader`) -> a placeholder Method keyed `pkg.Type.Method`.

    GUARD: a callee in a *project* package/type is intra-project dangling (a missing local symbol),
    NOT external -- left dropped so nothing stale survives a resync. A bare `foo()` names no
    package, so builtins (len/append/make) and same-package danglers are skipped."""
    if call.receiver is None:
        return None
    if call.receiver in member_types:  # receiver/param/field-typed method call
        split = _split_qualified_type(member_types[call.receiver])
        if split is None:
            return None
        qualifier, type_name = split
        if qualifier in f.import_paths and r.ip_for_qualifier(qualifier, f.import_paths) is None:
            import_path = f.import_paths[qualifier]
            qualified = f"{import_path}.{type_name}.{call.name}"
            return ids.extern_function_key(qualified), "Method", _external_props(call.name, qualified, import_path)
        return None
    # package-qualified free-function call
    if call.receiver in f.import_paths and r.ip_for_qualifier(call.receiver, f.import_paths) is None:
        import_path = f.import_paths[call.receiver]
        qualified = f"{import_path}.{call.name}"
        return ids.extern_function_key(qualified), "Function", _external_props(call.name, qualified, import_path)
    return None


def _external_props(short_name: str, qualified: str, import_path: str) -> dict:
    return {
        "name": short_name, "qualified_name": qualified, "resolved": False,
        "origin": _go_origin(import_path), "linkage_key": ids.extern_function_key(qualified),
    }


def _split_qualified_type(ref: str):
    """A written type text -> (pkg_qualifier, TypeName) if it is a simple `pkg.Type` (optionally
    behind a leading `*`/`&`), else None. Bracketed/mapped/nested forms (`[]io.Reader`,
    `map[string]io.Reader`, `a.b.C`) are left unnamed rather than guessed."""
    t = ref.lstrip("*&")
    if not t or "." not in t or any(c in t for c in "[]{}() \t"):
        return None
    qualifier, name = t.split(".", 1)
    if not qualifier or "." in name:
        return None
    return qualifier, name


# -- helpers -------------------------------------------------------------------------------

def _import_map(rf: RawFile) -> dict:
    """alias -> package name (an import path's last segment). For a non-aliased import the alias
    is that same last segment; aliased imports (`m "x/y/model"`) map the alias to "model"."""
    out: dict = {}
    for imp in rf.imports:
        seg = imp.path.rstrip("/").split("/")[-1]
        out[imp.alias or seg] = seg
    return out


def _import_paths(rf: RawFile) -> dict:
    """alias -> full import path, so a qualified call names its package's path (the extern placeholder
    key, whose first segment classifies stdlib vs third-party -- ADR 0004). An aliased import is keyed
    by its alias. A non-aliased one binds its package's `package` clause, which the fast pass can't
    read for a module outside the project, so it is keyed by its last path segment AND by the name Go
    tooling assumes (gomod.package_name: `chi` for `github.com/go-chi/chi/v5`). The assumed name is
    the weaker key: it never displaces an alias or a last segment, and two imports assuming the same
    name bind neither."""
    out: dict = {}
    assumed: dict[str, set[str]] = defaultdict(set)
    for imp in rf.imports:
        if imp.alias:
            out[imp.alias] = imp.path
            continue
        out[imp.path.rstrip("/").split("/")[-1]] = imp.path
        assumed[gomod.package_name(imp.path)].add(imp.path)
    for name, paths in assumed.items():
        if name and len(paths) == 1:
            out.setdefault(name, next(iter(paths)))
    return out


def _exported(name: str) -> bool:
    return bool(name) and name[0].isupper()


def _norm(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def _basename(path: str) -> str:
    return _norm(path).rsplit("/", 1)[-1]


def _dir_of(path: str) -> str:
    """The repo-relative directory of a file ("" for a repo-root file). One package per directory in
    Go, so the dir is (almost) the package identity -- see `_pkg_key` for the one exception."""
    norm = _norm(path)
    return norm.rsplit("/", 1)[0] if "/" in norm else ""


def _pkg_key(pkg_dir: str, clause: str) -> str:
    """The package node-id key: its directory, EXCEPT Go's external test package (`package foo_test`),
    which legally coexists with `package foo` in the same directory as a separate compilation unit --
    it gets a `#test` suffix so the two never merge. A directory has at most one external test package,
    so the suffix is a sufficient discriminator."""
    return f"{pkg_dir}#test" if clause.endswith("_test") else pkg_dir
