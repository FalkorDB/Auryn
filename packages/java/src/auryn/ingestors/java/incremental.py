"""Java binding for the AUTHORED-SHARED incremental resync engine (core/incremental_scoped.py,
ADR 0005) -- an aggregation-tier language like Go: a Class/Interface/Enum/Record/Method is keyed
`java://<proj>/module/<mod>/package/<pkg>...` (or `java://<proj>/package/...` for a root-level file,
which has no Module), NOT under a file, and the `Module` (Maven/Gradle subproject) and `Package`
nodes are aggregation points many files feed. The per-file ownership
handle is the `File-[:DEFINES]->entity` edge (added in
resolution.py, uniform with C/Go), emitted for every type AND method (a Java method is always
co-located with its type, so both source at the same File). No headers -> no declaration/definition
split (definition_rels == provenance_rels == ("DEFINES",)); Java forbids duplicate FQNs so the
multi-definer collision path is a no-op here.

Supplies the two language-specific pieces -- reconstructing Java's resolution tables from the graph
(`read_java_index`) and emitting a subset against them (`resolution.resolve_incremental`) -- plus the
declarative spec. Correctness (incremental == full) is enforced by the language-agnostic gate
(tests/sync/test_delta_differential.py + test_delta_cross_repo.py).
"""

from __future__ import annotations

from auryn.core.graph_client import SHARED_NODE_LABEL, GraphWriter, semantic_label
from auryn.core.graph_model import GraphNode
from auryn.core.incremental_scoped import ChangeSet, ScopedSpec, incremental_sync_authored
from auryn.ingestors.java import ids, javamod, resolution
from auryn.ingestors.java.file_parser import parse_source
from auryn.ingestors.java.plugin import EXCLUDED_DIR_NAMES as _EXCLUDED_DIR_NAMES
from auryn.ingestors.java.plugin import FILE_SUFFIXES as _FILE_SUFFIXES
from auryn.ingestors.java.schema import NODE_SCHEMAS

__all__ = ["ChangeSet", "JAVA_SPEC", "read_java_index", "incremental_java_sync"]

# Edge classification (see core/incremental_scoped.read_authored_fingerprints):
#  - provenance (File→entity ownership handle): DEFINES (types + methods).
#  - lateral (source-owned): CALLS + DEPENDS_ON + EXTENDS + IMPLEMENTS + DEFINED_IN. DEFINED_IN
#    (type->its File) is source-owned by the type: when the type MOVES to another file its old DEFINED_IN
#    must be diffed away, so it has to be read back with the type -- else the stale edge survives
#    (incremental != full).
#  - containment (target-owned): the structural spine (incl. HAS_PACKAGE/HAS_FILE).
#  - symbol-ref (reverse-dep closure): a change to a type/method affects its callers/subtypes/
#    implementers/dependents -> CALLS + DEPENDS_ON + EXTENDS + IMPLEMENTS (NOT DEFINED_IN: moving a type's
#    file doesn't change the type node, so dependents need no re-resolve).
_PROVENANCE_RELS = ("DEFINES",)
_LATERAL_RELS = ("CALLS", "DEPENDS_ON", "EXTENDS", "IMPLEMENTS", "DEFINED_IN")
_CONTAINMENT_RELS = ("HAS_MODULE", "HAS_PACKAGE", "HAS_SUBPACKAGE", "HAS_FILE", "HAS_TYPE", "HAS_NESTED_TYPE",
                     "HAS_METHOD", "HAS_FIELD", "HAS_PARAMETER", "HAS_ENUM_MEMBER")
_SYMBOL_REF_RELS = ("CALLS", "DEPENDS_ON", "EXTENDS", "IMPLEMENTS")
_TARGETABLE = ("Class", "Interface", "Enum", "Record", "Method")


def _scope_key(path: str) -> str:
    """A file's stable scope key = normalized path -- exactly what `File.path`/`file_id` use."""
    return path.replace("\\", "/").lstrip("./")


def _java_project_node(project_name, repo_uri, proj_id, target_sha, ref) -> GraphNode:
    return GraphNode("Project", {"id": proj_id},
                     {"name": project_name, "repo_uri": repo_uri, "commit_sha": target_sha, "ref": ref})


def _walk_types(rf):
    """Yield (type, package) for every type in a file, recursing into nested types (which share the
    file's package, as the emitter models them)."""
    def walk(rt):
        yield rt
        for nested in rt.nested_types:
            yield from walk(nested)
    for rt in rf.types:
        yield from walk(rt)


def _java_referenced_names(rf) -> set[str]:
    """Simple names an emit file references cross-file (call names, super/interface names, param/
    return/field type tokens). Only bounds the seed; read_java_index seeds the whole non-emit index,
    so over-collecting is harmless."""
    names: set[str] = set()
    for rt in _walk_types(rf):
        if rt.superclass:
            names.add(rt.superclass)
        names.update(rt.interfaces)
        for m in rt.methods:
            names.update(c.name for c in m.calls)
            names.update(resolution._referenced_names(m.return_type))
            for p in m.parameters:
                names.update(resolution._referenced_names(p.type))
        for f in rt.fields:
            names.update(resolution._referenced_names(f.type))
    return {n for n in names if n}


def _java_satisfied_linkage_keys(raws) -> set[str]:
    """The extern:// linkages the emit set's externally-visible methods can satisfy: a caller pointing
    at `extern://java/<owner-fqcn>#<method>` (a type that wasn't in this project when it resolved)
    re-binds when an emit file adds that public/protected method. Mirrors _emit_method's key exactly."""
    out: set[str] = set()
    for rf in raws:
        for rt in _walk_types(rf):
            fqcn = f"{rt.package}.{rt.name}" if rt.package else rt.name
            for m in rt.methods:
                if m.accessibility in ("public", "protected"):
                    out.add(ids.extern_method_key(f"{fqcn}#{m.name}"))
    return out


def _java_displaced_caller_files(writer: GraphWriter, proj_id: str, emit_raws) -> set[str]:
    """Finding-3 displacement (ADR 0005): a newly-ADDED type can out-bias a reference already bound to
    a DIFFERENT type of the same SIMPLE NAME. The resolver matches by simple name with a precedence --
    same-module+package, then same-package, then same-module, then any (by id) -- so adding `Helper`
    anywhere can retarget an EXTENDS/IMPLEMENTS/DEPENDS_ON that pointed at another `Helper` (and, via a
    receiver's field/parameter type, a CALLS). Neither the reverse-dep nor the placeholder closure
    revisits such a reference -- it points at a real node, not an extern:// placeholder -- so without
    this it stays stale (full != incremental).

    Re-emit EXACTLY the files whose definitions reference a project type of an added simple name, so
    they re-resolve as a full recompute would. Keying on the real reference edges (not on a scope
    guess) makes it correct across every precedence tier at once -- a root-scoped caller, a same-module
    caller in another package, a cross-module caller -- and precise: only files that actually reference
    the name are pulled. A no-op when the edit adds no NEW type (a body edit keeps the same id, so it's
    excluded) or when nothing references the added name."""
    lo, hi = proj_id + "/", proj_id + "/" + chr(0xFFFF)
    # (type_id, simple name) of every type the emit set defines (nested included).
    defined: list[tuple[str, str]] = []
    for raw in emit_raws:
        module = javamod.module_for_path(raw.path)
        scope_root = resolution._scope_root(proj_id, module)
        entries: list = []
        for rt in raw.types:
            resolution._flatten(rt, "", scope_root, None, raw.package, entries, module)
        defined.extend((e.type_id, e.name) for e in entries)
    if not defined:
        return set()
    # Only a genuinely ADDED type (id not already in the graph) can change an existing reference's
    # preferred target; a body edit reuses the id, so it's skipped -- no needless reparse (PR #9 #3).
    # Match the shared :Entity label (every node carries it) so this is an id-INDEX seek over just the
    # emitted ids, not a full-graph scan -- the hook runs on every Java edit, including body-only ones.
    existing = {str(tid) for (tid,) in writer.ro_query(
        f"MATCH (t:{SHARED_NODE_LABEL}) WHERE t.id IN $ids RETURN t.id", {"ids": [t for t, _ in defined]}).result_set}
    added_names = {name for tid, name in defined if tid not in existing}
    if not added_names:
        return set()
    names = list(added_names)
    # (a) Files whose definitions reference a project type of that simple name via a TYPE edge. The
    # referencing source is a Type (EXTENDS/IMPLEMENTS -> its own DEFINED_IN file) or a Method/Field
    # whose owning Type has DEFINED_IN (DEPENDS_ON -- a param/field/return type ref).
    rows = writer.ro_query(
        "MATCH (src)-[:EXTENDS|IMPLEMENTS|DEPENDS_ON]->(t) "
        "WHERE t.name IN $names AND (t:Class OR t:Interface OR t:Enum OR t:Record) "
        "AND t.id >= $lo AND t.id < $hi "
        "OPTIONAL MATCH (src)-[:DEFINED_IN]->(f1:File) "
        "OPTIONAL MATCH (owner)-[:HAS_METHOD|HAS_FIELD]->(src), (owner)-[:DEFINED_IN]->(f2:File) "
        "RETURN f1.id, f2.id",
        {"names": names, "lo": lo, "hi": hi},
    ).result_set
    out = {str(fid) for row in rows for fid in row if fid is not None}
    # (b) Constructor-ONLY callers: `new X()` is a CALLS to X's constructor Method, NOT a DEPENDS_ON,
    # so a method that merely constructs X (no field/param/extends of it) is invisible to (a). Follow
    # the CALLS through the constructor's owning type to the caller's file (PR #9 follow-up #1).
    ctor_rows = writer.ro_query(
        "MATCH (f:File)-[:DEFINES]->(:Method)-[:CALLS]->(ctor:Method)<-[:HAS_METHOD]-(t) "
        "WHERE ctor.kind = 'CONSTRUCTOR' AND t.name IN $names "
        "AND (t:Class OR t:Interface OR t:Enum OR t:Record) "
        "AND t.id >= $lo AND t.id < $hi RETURN DISTINCT f.id",
        {"names": names, "lo": lo, "hi": hi},
    ).result_set
    return out | {str(fid) for (fid,) in ctor_rows if fid is not None}


def _java_defined_symbol_ids(raw, proj_id: str) -> list[tuple[str, str]]:
    """(node_id, label) of the program-global symbols this file defines -- types and their methods --
    via the SAME id functions the emitter uses (drives v2 reverse-dep narrowing). Methods CAN be
    included (unlike Go): a Java method's id depends only on its type, which is always in the same
    file, so it's precisely computable from the parse. The module scope root is path-derived (same
    rule as the full resolve), so the ids match even though this callback never sees the source body."""
    module = javamod.module_for_path(raw.path)
    scope_root = resolution._scope_root(proj_id, module)
    entries: list = []
    for rt in raw.types:
        resolution._flatten(rt, "", scope_root, None, raw.package, entries, module)
    out: list[tuple[str, str]] = []
    for e in entries:
        out.append((e.type_id, e.kind))
        for m in e.raw_type.methods:
            out.append((resolution._method_id(e.type_id, m), "Method"))
    return out


def read_java_index(writer: GraphWriter, proj_id: str, exclude_keys: set[str], names: set[str]) -> resolution.JavaGraphIndex:
    """Reconstruct Java's resolution tables from the graph for every file EXCEPT `exclude_keys` (the
    emit + removed set), so `resolve_incremental` resolves an emit file's cross-file/cross-package
    refs without re-parsing the project. Types come back as lightweight stubs (id/kind/name/package +
    a constructor id for `new` targets); method_index carries the graph method ids directly (no
    recompute). Every query is an id-index seek."""
    idx = resolution.JavaGraphIndex()
    lo, hi = proj_id + "/", proj_id + "/" + chr(0xFFFF)
    exclude_file_ids = {ids.file_id(proj_id, k) for k in exclude_keys}
    seed_file_ids = [
        str(fid) for (fid,) in writer.ro_query(
            "MATCH (f:File) WHERE f.id >= $lo AND f.id < $hi RETURN f.id", {"lo": lo, "hi": hi}
        ).result_set if str(fid) not in exclude_file_ids
    ]

    idx.packages = {
        str(name) for (name,) in writer.ro_query(
            "MATCH (p:Package) WHERE p.id >= $lo AND p.id < $hi RETURN p.name", {"lo": lo, "hi": hi}
        ).result_set
    }
    if not seed_file_ids:
        return idx

    # A `new X()` on a non-emit type needs X's constructor id (the emitter derives it from the raw
    # body, absent in a stub) -> reconstruct type_id -> constructor method id. Must match the full
    # resolve, which binds `new X(...)` to `next((m for m in raw_type.methods if m.kind=='CONSTRUCTOR'))`
    # -- the FIRST constructor in SOURCE order, ignoring the call's args. So order by line_start
    # (ascending) and keep the first per type; an unordered query would pick an arbitrary overload
    # for a multi-ctor type (e.g. JsonIOException(String) vs (Throwable)) and diverge from full.
    ctor_by_type: dict[str, str] = {}
    for tid, mid in writer.ro_query(
        "MATCH (file:File)-[:DEFINES]->(m:Method), (t)-[:HAS_METHOD]->(m) "
        "WHERE file.id IN $fids AND m.kind = 'CONSTRUCTOR' RETURN t.id, m.id ORDER BY m.line_start",
        {"fids": seed_file_ids},
    ).result_set:
        ctor_by_type.setdefault(str(tid), str(mid))

    for tid, label, tname, qn, module in writer.ro_query(
        f"MATCH (file:File)-[:DEFINES]->(t) WHERE file.id IN $fids "
        f"AND (t:Class OR t:Interface OR t:Enum OR t:Record) "
        f"RETURN DISTINCT t.id, {semantic_label('t')}, t.name, t.qualified_name, t.module", {"fids": seed_file_ids},
    ).result_set:
        tid, label, tname = str(tid), str(label), str(tname)
        qn = str(qn) if qn is not None else tname
        pkg = qn.rsplit(".", 1)[0] if "." in qn else ""
        idx.type_index.setdefault(tname, []).append(
            resolution.make_type_stub(tid, label, tname, pkg, str(module) if module is not None else "", ctor_by_type.get(tid))
        )

    # method_index carries the owner type's module (via t.module) so an incremental resolve biases
    # same-module method binding exactly as the full resolve does (keeps two modules' same-package
    # methods from cross-binding).
    for mname, owner_id, mid, qn, module in writer.ro_query(
        "MATCH (file:File)-[:DEFINES]->(m:Method), (t)-[:HAS_METHOD]->(m) "
        "WHERE file.id IN $fids RETURN m.name, t.id, m.id, t.qualified_name, t.module", {"fids": seed_file_ids},
    ).result_set:
        qn = str(qn) if qn is not None else ""
        pkg = qn.rsplit(".", 1)[0] if "." in qn else ""
        idx.method_index.setdefault(str(mname), []).append(
            (str(owner_id), str(mid), pkg, str(module) if module is not None else ""))
    return idx


JAVA_SPEC = ScopedSpec(
    language="java",
    node_schemas=tuple(NODE_SCHEMAS),
    node_labels=tuple(s.label for s in NODE_SCHEMAS),
    file_suffixes=tuple(_FILE_SUFFIXES),
    excluded_dir_names=frozenset(_EXCLUDED_DIR_NAMES),
    symbol_ref_rels=_SYMBOL_REF_RELS,
    lateral_rels=_LATERAL_RELS,
    containment_rels=_CONTAINMENT_RELS,
    targetable_labels=_TARGETABLE,
    import_rel=None,  # no IMPORTS edge; cross-package/repo unresolved calls are extern:// placeholders
    import_target_labels=(),
    project_id=ids.project_id,
    scope_id_for_path=ids.file_id,
    scope_key_for_path=_scope_key,
    parse=parse_source,
    is_broken=lambda rf: False,  # tree-sitter best-effort; identical in both engines (matches C/Go/Bash)
    referenced_names=_java_referenced_names,
    read_index=read_java_index,
    resolve_incremental=resolution.resolve_incremental,
    project_node=_java_project_node,
    satisfied_linkage_keys=_java_satisfied_linkage_keys,
    # A newly-added type can out-bias a reference already bound cross-module (two Gradle modules
    # sharing a package); re-emit the shadowed module/package so it re-resolves (ADR 0005). No-op
    # unless subprojects with a duplicated FQCN are in play.
    displaced_caller_files=_java_displaced_caller_files,
    provenance_rels=_PROVENANCE_RELS,
    definition_rels=("DEFINES",),  # no headers -> no declaration/definition split
    linkable_labels=("Method",),
    defined_symbol_ids=_java_defined_symbol_ids,
    # A Package prefix (e.g. `com`) -- and the Module it nests under -- is co-declared by every file
    # beneath it; props are id-derived, so both are excluded from the co-DEFINER winner closure (editing
    # one file must not pull the whole subtree). They stay provenance-owned (File-DEFINES-Package/Module)
    # for refcount GC + in the co-contributor closure, so a module/package is reclaimed with its last file.
    aggregation_labels=("Package", "Module"),
)


def incremental_java_sync(writer, source, target_sha, changes, *, project_name, repo_uri, ref=None):
    """Thin wrapper over the shared authored-shared engine (git_sync routes through the same one)."""
    return incremental_sync_authored(writer, JAVA_SPEC, source, target_sha, changes,
                                     project_name=project_name, repo_uri=repo_uri, ref=ref)
