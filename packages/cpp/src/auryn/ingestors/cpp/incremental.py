"""C++ binding for the AUTHORED-SHARED incremental resync engine (core/incremental_scoped.py,
ADR 0005).

C++ is the structural union of C and Java (see resolution.py / schema.py), so its incremental spec is
too: from Java, a `Namespace` aggregation tier over types with methods/fields and an EXTENDS hierarchy;
from C, free `Function`s, file-scoped `Macro`s, and File->File `INCLUDES` + the extern:// call-placeholder
model. Almost every entity is program-global (keyed `cpp://<proj>/namespace/.../class.X/method.Y`, NOT under
a file), shared across the header that DECLARES it and the .cc that DEFINES it; the per-file ownership
handle is the `File-[:DEFINES|DECLARES]->entity` edge (added in resolution.py, uniform with C/Go/Java).

The one feature no other authored language has is OUT-OF-LINE method definitions: a method is declared in a
class body (a .h) but defined `Class::method(){..}` in a separate .cc. So a single method node is co-authored
by two different files. Both must be re-authored together on any resync, which the engine's two co-member
closures guarantee: `type_member_rels` (forward: a type's .h changes -> pull the method's .cc) and
`member_owner_rels` (reverse: the .cc changes -> pull the type's .h). With both, a method's complete
contributor set is always in the emit set, so it is never sparsely re-emitted -- no decl-only handling is
needed for methods (only for forward-declared TYPES and free-function PROTOTYPES, as in C).

Correctness (incremental == full recompute, node/edge-identical) is enforced by the language-agnostic gate
(tests/sync/test_delta_differential.py + test_delta_cross_repo.py)."""

from __future__ import annotations

from auryn.core.cpp_headers import HEADER_SUFFIX, is_cpp_header_blob
from auryn.core.graph_client import GraphWriter, semantic_label
from auryn.core.graph_model import GraphNode
from auryn.core.incremental_scoped import ChangeSet, ScopedSpec, incremental_sync_authored
from auryn.ingestors.cpp import ids, resolution
from auryn.ingestors.cpp.file_parser import parse_source
from auryn.ingestors.cpp.plugin import EXCLUDED_DIR_NAMES as _EXCLUDED_DIR_NAMES
from auryn.ingestors.cpp.plugin import FILE_SUFFIXES as _FILE_SUFFIXES
from auryn.ingestors.cpp.schema import NODE_SCHEMAS

__all__ = ["ChangeSet", "CPP_SPEC", "read_cpp_index", "incremental_cpp_sync"]

# Edge classification for the engine (see core/incremental_scoped.read_authored_fingerprints):
#  - provenance (File->entity ownership handle): DEFINES (definitions) / DECLARES (header-only).
#  - lateral (source-owned): CALLS + DEPENDS_ON + EXTENDS + INCLUDES + DEFINED_IN (type->File, re-emitted
#    with its type).
#  - containment (target-owned): the structural spine.
#  - symbol-ref (reverse-dep closure): a change to a type/method/function affects its callers / subclasses
#    / dependents -> CALLS + DEPENDS_ON + EXTENDS.
_PROVENANCE_RELS = ("DEFINES", "DECLARES")
_LATERAL_RELS = ("CALLS", "DEPENDS_ON", "EXTENDS", "INCLUDES", "DEFINED_IN")
_CONTAINMENT_RELS = ("HAS_NAMESPACE", "HAS_SUBNAMESPACE", "HAS_FILE", "HAS_TYPE", "HAS_NESTED_TYPE",
                     "HAS_FUNCTION", "HAS_METHOD", "HAS_FIELD", "HAS_PARAMETER", "HAS_ENUM_MEMBER")
_SYMBOL_REF_RELS = ("CALLS", "DEPENDS_ON", "EXTENDS")
_TARGETABLE = ("Class", "Struct", "Union", "Enum", "Method", "Function")
_TYPE_LABEL_PRED = "(t:Class OR t:Struct OR t:Union OR t:Enum)"


def _scope_key(path: str) -> str:
    """A file's stable scope key = normalized path -- exactly what `File.path`/`file_id` use."""
    return path.replace("\\", "/").lstrip("./")


def _cpp_project_node(project_name, repo_uri, proj_id, target_sha, ref) -> GraphNode:
    return GraphNode("Project", {"id": proj_id},
                     {"name": project_name, "repo_uri": repo_uri, "commit_sha": target_sha, "ref": ref})


def _namespace_of(qualified_name: str | None, name: str) -> str:
    """The `::`-joined namespace of a qualified_name (`a::b::X` -> `a::b`), "" for global scope."""
    qn = qualified_name if qualified_name is not None else name
    return qn.rsplit("::", 1)[0] if "::" in qn else ""


def _cpp_referenced_names(rf) -> set[str]:
    """Simple names an emit file could resolve cross-file: call names, base names, and the identifiers
    in method/function/field/param/return type text. Only bounds the index seed; read_cpp_index seeds
    the whole non-emit index, so over-collecting is harmless."""
    names: set[str] = set()

    def add_type_text(txt):
        names.update(resolution._referenced_names(txt))

    for rt in rf.types:
        _collect_type_names(rt, names, add_type_text)
    for fn in rf.functions:
        add_type_text(fn.return_type)
        for p in fn.params:
            add_type_text(p.type)
        names.update(c.name for c in fn.calls)
        names.update(c.scope for c in fn.calls if c.scope)
    for md in rf.member_defs:
        if md.owner_name:
            names.add(md.owner_name)
        add_type_text(md.return_type)
        for p in md.params:
            add_type_text(p.type)
        names.update(c.name for c in md.calls)
        names.update(c.scope for c in md.calls if c.scope)
    return {n for n in names if n}


def _collect_type_names(rt, names: set[str], add_type_text) -> None:
    for b in rt.bases:
        names.add(b.name)
    for m in rt.methods:
        add_type_text(m.return_type)
        for p in m.params:
            add_type_text(p.type)
        names.update(c.name for c in m.calls)
        names.update(c.scope for c in m.calls if c.scope)
    for f in rt.fields:
        add_type_text(f.type)
    for nested in rt.nested_types:
        _collect_type_names(nested, names, add_type_text)


def _cpp_defined_symbol_ids(raw, proj_id: str) -> list[tuple[str, str]]:
    """(node_id, label) of the program-global symbols this file defines, computed from the parse via the
    SAME id functions the emitter uses -- types, their in-class methods, and free functions. Drives the v2
    reverse-dep narrowing. OUT-OF-LINE method definitions (whose owner type is cross-file and so not
    resolvable from this file alone) are deliberately OMITTED: that only makes the narrowing more
    conservative (an out-of-line method reads as "removed", harmlessly re-resolving its callers and pulling
    its class's file -- which the member_owner closure pulls anyway), never less safe."""
    out: list[tuple[str, str]] = []
    entries: list = []
    for rt in raw.types:
        resolution._flatten(rt, "", proj_id, None, entries)
    for e in entries:
        out.append((e.type_id, e.kind))
        for m in e.raw_type.methods:
            out.append((ids.method_id(e.type_id, m.name, [p.type for p in m.params]), "Method"))
    for fn in raw.functions:
        out.append((resolution._make_func_rec(fn, proj_id).function_id, "Function"))
    return out


def _cpp_satisfied_linkage_keys(raws) -> set[str]:
    """The extern:// linkages the emit set's free-function definitions can satisfy: an untouched caller
    pointing at `extern://cpp/<qualified>` re-binds when an emit file defines that free function. Mirrors
    _emit_function's linkage_key exactly. (Methods carry no linkage_key -- C++ cross-repo calls are
    free/namespaced functions only, as in resolution._external_free_call.)"""
    out: set[str] = set()
    for rf in raws:
        for fn in rf.functions:
            qualified = f"{fn.namespace}::{fn.name}" if fn.namespace else fn.name
            out.add(ids.extern_function_key(qualified))
    return out


def _cpp_displaced_emit_files(writer: GraphWriter, proj_id: str, raws) -> set[str]:
    """Extra emit files a PARSE-DRIVEN lookup finds that the graph-provenance closures miss because the
    emit file is BRAND NEW (its own edges aren't in the graph yet, ADR 0005). The C++ out-of-line case: a
    new .cc with `Class::method(){..}` -- `_comember_owner_file_ids` can't pull `Class`'s file because it
    keys on the new .cc's not-yet-existing provenance. Pull the owner type's WHOLE contributor set: the
    file(s) that DEFINE/DECLARE the type AND the file(s) defining its OTHER methods. Pulling just the header
    is not enough -- re-authoring the type from its header alone would downgrade its other out-of-line
    methods (defined in .cc files not pulled) to declaration-only. This replicates what the co-member
    fixpoint does for an existing file; it must be explicit here because a brand-new .cc has no provenance
    to seed that fixpoint. Returns File ids; the engine parses + adds them to the emit set. A no-op for the
    common edit (no new out-of-line def). Every query is an id-index seek.

    KNOWN LIMITATION (documented, not handled): adding a BRAND-NEW file that re-defines a free function an
    existing file already defines can pick a different multiply-defined winner than a full recompute (the
    co-definer closure keys on the new file's absent provenance). This is an ODR violation in C++ (two
    external definitions of one free function don't link), so it is pathological; the normal multi-variant
    case where the colliding files already EXIST is handled by the graph co-definer closure + path-sorting."""
    lo, hi = proj_id + "/", proj_id + "/" + chr(0xFFFF)
    out: set[str] = set()
    owner_names = sorted({md.owner_name for raw in raws for md in raw.member_defs if md.owner_name})
    if owner_names:
        for (fid,) in writer.ro_query(
            f"MATCH (f:File)-[:DEFINES|DECLARES]->(t) WHERE t.name IN $names AND {_TYPE_LABEL_PRED} "
            f"AND t.id >= $lo AND t.id < $hi RETURN DISTINCT f.id",
            {"names": owner_names, "lo": lo, "hi": hi},
        ).result_set:
            out.add(str(fid))
        for (fid,) in writer.ro_query(
            f"MATCH (f:File)-[:DEFINES]->(m:Method), (t)-[:HAS_METHOD]->(m) "
            f"WHERE t.name IN $names AND {_TYPE_LABEL_PRED} AND t.id >= $lo AND t.id < $hi "
            f"RETURN DISTINCT f.id",
            {"names": owner_names, "lo": lo, "hi": hi},
        ).result_set:
            out.add(str(fid))
    # Co-DECLARER pull: for each TYPE the emit set defines, pull the file(s) that merely DECLARE it (a
    # `class X;` forward declaration in another header). DEFINED_IN (type->File) is a lateral multi-file
    # edge -- a type declared in a header AND defined in a .cc has one per file -- and the read-back claims
    # ALL of them once the type is owned. The co-DEFINER closure is DEFINES-keyed and so misses a pure
    # forward-declarer; without this, editing the DEFINER .cc re-emits only its own DEFINED_IN and
    # compute_delta drops the declarer's (and can strand the type's other members). A no-op unless the
    # emit set defines a type some other file forward-declares.
    # Pull each such type's WHOLE contributor set: its forward-declarer header(s) AND the file(s) defining
    # its out-of-line methods. Pulling only the declarer isn't enough -- re-authoring the type without its
    # method-definer .cc would merge the methods from their DECLARATION (decl param NAMES), while a full
    # recompute uses the DEFINITION's (so a param `batch` vs an unnamed `arg3` strands a Parameter node).
    type_ids = [sid for raw in raws for sid, lbl in _cpp_defined_symbol_ids(raw, proj_id)
                if lbl in ("Class", "Struct", "Union", "Enum")]
    for i in range(0, len(type_ids), 1000):
        chunk = type_ids[i:i + 1000]
        for (fid,) in writer.ro_query(
            "MATCH (f:File)-[:DECLARES]->(t) WHERE t.id IN $ids RETURN DISTINCT f.id", {"ids": chunk},
        ).result_set:
            out.add(str(fid))
        for (fid,) in writer.ro_query(
            "MATCH (f:File)-[:DEFINES]->(m:Method)<-[:HAS_METHOD]-(t) WHERE t.id IN $ids "
            "RETURN DISTINCT f.id", {"ids": chunk},
        ).result_set:
            out.add(str(fid))
    return out


def read_cpp_index(writer: GraphWriter, proj_id: str, exclude_keys: set[str], names: set[str]) -> resolution.CppGraphIndex:
    """Reconstruct C++'s resolution tables from the graph for every file EXCEPT `exclude_keys` (the emit +
    removed set), so `resolve_incremental` resolves an emit file's cross-file EXTENDS/DEPENDS_ON/CALLS/
    #include against the surviving project without re-parsing it. Every query is an id-index seek."""
    idx = resolution.CppGraphIndex()
    lo, hi = proj_id + "/", proj_id + "/" + chr(0xFFFF)
    exclude_file_ids = {ids.file_id(proj_id, k) for k in exclude_keys}

    # Surviving files' normalized full path -> File id, for #include resolution. NO basename collapse here:
    # a basename shared by a surviving file and an emit file must be judged ambiguous over the UNION, so
    # resolve_incremental collapses basenames over seed+emit together (see resolution._collapse_basenames).
    full: dict[str, str] = {}
    seed_file_ids: list[str] = []
    for fid, path in writer.ro_query(
        "MATCH (f:File) WHERE f.id >= $lo AND f.id < $hi RETURN f.id, f.path", {"lo": lo, "hi": hi}
    ).result_set:
        if str(fid) in exclude_file_ids:
            continue
        seed_file_ids.append(str(fid))
        full[resolution._norm(str(path))] = str(fid)
    idx.include_fullpaths = full

    if not seed_file_ids:
        return idx

    # Type stubs (name -> [_TypeStub]) for EXTENDS/DEPENDS_ON/call-receiver resolution.
    for tid, label, tname, qn in writer.ro_query(
        f"MATCH (file:File)-[:DEFINES|DECLARES]->(t) WHERE file.id IN $fids AND {_TYPE_LABEL_PRED} "
        f"RETURN DISTINCT t.id, {semantic_label('t')}, t.name, t.qualified_name", {"fids": seed_file_ids},
    ).result_set:
        tname = str(tname)
        idx.type_index.setdefault(tname, []).append(
            resolution._TypeStub(str(tid), str(label), tname, _namespace_of(qn, tname)))

    # Method entries (name -> [_MethodIndexEntry], incl constructors) for CALLS / `new X()` resolution.
    for mname, owner_id, mid, kind, owner_qn in writer.ro_query(
        "MATCH (file:File)-[:DEFINES|DECLARES]->(m:Method), (t)-[:HAS_METHOD]->(m) "
        "WHERE file.id IN $fids RETURN DISTINCT m.name, t.id, m.id, m.kind, t.qualified_name",
        {"fids": seed_file_ids},
    ).result_set:
        idx.method_index.setdefault(str(mname), []).append(
            resolution._MethodIndexEntry(str(owner_id), str(mid), str(kind), _namespace_of(owner_qn, "")))

    # Free-function stubs (name -> [_FuncStub]) for free-function CALLS resolution.
    for fname, fnid, qn in writer.ro_query(
        "MATCH (file:File)-[:DEFINES|DECLARES]->(fn:Function) WHERE file.id IN $fids "
        "RETURN DISTINCT fn.name, fn.id, fn.qualified_name", {"fids": seed_file_ids},
    ).result_set:
        fname = str(fname)
        idx.function_index.setdefault(fname, []).append(
            resolution._FuncStub(str(fnid), _namespace_of(qn, fname)))

    # ids DEFINED (not merely declared) by a surviving file: a forward-decl / prototype in the emit set
    # for one of these emits only its DECLARES provenance, never a sparse node (ADR 0005 ownership).
    idx.defined_type_ids = frozenset(
        str(tid) for (tid,) in writer.ro_query(
            f"MATCH (file:File)-[:DEFINES]->(t) WHERE file.id IN $fids AND {_TYPE_LABEL_PRED} "
            f"RETURN DISTINCT t.id", {"fids": seed_file_ids}).result_set)
    idx.defined_function_ids = frozenset(
        str(fnid) for (fnid,) in writer.ro_query(
            "MATCH (file:File)-[:DEFINES]->(fn:Function) WHERE file.id IN $fids "
            "RETURN DISTINCT fn.id", {"fids": seed_file_ids}).result_set)
    return idx


def _cpp_claims_path(path: str, read_blob) -> bool:
    """C++ owns `.cpp/.cc/.hpp/...` always, and a `.h` ONLY if its content is C++ (header routing, see
    core.cpp_headers). Mirrors the full-ingest walk in git_sync (which claims a C++-content `.h`) and C's
    inverse disown, so a header's owner is identical on a full ingest and an incremental resync -- and,
    because `.h` is in CPP_SPEC.file_suffixes below, the incremental diff actually SEES a changed C++ `.h`
    (without this glue the C++ incremental resync would never re-ingest an edited class header)."""
    if path.endswith(HEADER_SUFFIX):  # ".h" -- ambiguous C/C++; C++ claims only the C++-content ones
        return is_cpp_header_blob(read_blob())
    return True


CPP_SPEC = ScopedSpec(
    language="cpp",
    node_schemas=tuple(NODE_SCHEMAS),
    node_labels=tuple(s.label for s in NODE_SCHEMAS),
    # `.h` is added so the incremental diff detects a changed C++-content header (header routing sends it
    # to C++); `claims_path` then keeps only the C++-content `.h` (a C-content `.h` is disowned, exactly
    # as the full walk + the C ingestor's inverse do), so full-ingest and delta agree on ownership.
    file_suffixes=tuple(_FILE_SUFFIXES) + (HEADER_SUFFIX,),
    claims_path=_cpp_claims_path,
    excluded_dir_names=frozenset(_EXCLUDED_DIR_NAMES),
    symbol_ref_rels=_SYMBOL_REF_RELS,
    lateral_rels=_LATERAL_RELS,
    containment_rels=_CONTAINMENT_RELS,
    targetable_labels=_TARGETABLE,
    import_rel="INCLUDES",
    import_target_labels=("File",),
    project_id=ids.project_id,
    scope_id_for_path=ids.file_id,
    scope_key_for_path=_scope_key,
    parse=parse_source,
    # tree-sitter-cpp recovers from errors (best-effort partial), identical in both engines (matches C).
    is_broken=lambda rf: False,
    referenced_names=_cpp_referenced_names,
    read_index=read_cpp_index,
    resolve_incremental=resolution.resolve_incremental,
    project_node=_cpp_project_node,
    satisfied_linkage_keys=_cpp_satisfied_linkage_keys,
    # A BRAND-NEW .cc defining an existing class's method (or redefining an existing free function) has no
    # graph provenance yet, so the graph-keyed co-member/co-definer closures miss it; this parse-driven
    # hook pulls the owner type's file + any existing co-definer so the out-of-line method merges and a
    # multiply-defined symbol reduces to the same winner as a full recompute (PR #18 review).
    displaced_caller_files=_cpp_displaced_emit_files,
    provenance_rels=_PROVENANCE_RELS,
    definition_rels=("DEFINES",),  # a header DECLARES; the .cc / class-body DEFINES the data
    linkable_labels=("Function",),  # only free functions carry a cross-repo linkage_key (ADR 0004)
    defined_symbol_ids=_cpp_defined_symbol_ids,
    # Out-of-line method defs live in a .cc apart from the class body in a .h -> a method is co-authored by
    # two files. Pull BOTH together on any resync: type_member_rels (type's .h changes -> pull method .cc)
    # and member_owner_rels (the .cc changes -> pull the type's .h). Together a method is always re-authored
    # by its complete contributor set, so it is never sparsely re-emitted.
    type_member_rels=("HAS_METHOD",),
    member_owner_rels=("HAS_METHOD",),
    # Namespaces are an aggregation tier (like Java's Package/Module): id-derived props, co-declared by
    # every file beneath them -> refcount-GC'd, excluded from the co-definer winner closure.
    aggregation_labels=("Namespace",),
)


def incremental_cpp_sync(writer, source, target_sha, changes, *, project_name, repo_uri, ref=None):
    """Thin wrapper over the shared authored-shared engine (git_sync routes through the same one)."""
    return incremental_sync_authored(writer, CPP_SPEC, source, target_sha, changes,
                                     project_name=project_name, repo_uri=repo_uri, ref=ref)
