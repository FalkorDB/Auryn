"""Go binding for the AUTHORED-SHARED incremental resync engine (core/incremental_scoped.py,
ADR 0005) -- the second language on it after C, validating that the engine + its three fixes
(definition-vs-declaration ownership, order-independent multi-definer winner, v2 blast-radius
narrowing) carry over with only per-language glue and NO core changes.

Go is authored-shared like C but package-scoped rather than program-global: a Function/Method/
type/Const is keyed `go://<proj>/package/<pkg>...`, NOT under a file, and the flat `Package` node
is an aggregation point many files feed. So the per-file ownership handle is the
`File-[:DEFINES]->entity` edge (added in resolution.py, uniform with C). Go has no headers, so there
is no declaration/definition split -- definition_rels == provenance_rels == ("DEFINES",). It shares
C's multi-program collision (several `package main`/`func main` across cmd dirs), handled by the
same file-path sort + co-definer closure.

Supplies the two language-specific pieces -- reconstructing Go's resolution tables from the graph
(`read_go_index`) and emitting a subset against them (`resolution.resolve_incremental`) -- plus the
declarative spec. Correctness (incremental == full) is enforced by tests/sync/test_delta_differential.
"""

from __future__ import annotations

from collections import defaultdict

from auryn.core.graph_client import GraphWriter, semantic_label
from auryn.core.graph_model import GraphNode
from auryn.core.incremental_scoped import ChangeSet, ScopedSpec, incremental_sync_authored
from auryn.ingestors.go import ids, resolution
from auryn.ingestors.go.file_parser import parse_source
from auryn.ingestors.go.plugin import EXCLUDED_DIR_NAMES as _EXCLUDED_DIR_NAMES
from auryn.ingestors.go.plugin import FILE_SUFFIXES as _FILE_SUFFIXES
from auryn.ingestors.go.raw_model import RawField
from auryn.ingestors.go.schema import NODE_SCHEMAS

__all__ = ["ChangeSet", "GO_SPEC", "read_go_index", "incremental_go_sync"]

# Edge classification (see core/incremental_scoped.read_authored_fingerprints):
#  - provenance (File→entity ownership handle): DEFINES.
#  - lateral (source-owned): CALLS + DEPENDS_ON + EMBEDS + DEFINED_IN (all sourced at a func/method/
#    type/field). DEFINED_IN (type->its File) is source-owned by the type: when the type MOVES to another
#    file its old DEFINED_IN must be diffed away, so it has to be read back with the type -- else the stale
#    edge survives (incremental != full; seen moving a type to a new errors.go in spf13/viper).
#  - containment (target-owned): the structural spine, incl. HAS_PACKAGE/HAS_FILE.
#  - symbol-ref (reverse-dep closure): a change to a type/func/method affects its callers/dependents/
#    embedders -> CALLS + DEPENDS_ON + EMBEDS (NOT DEFINED_IN: moving a type's file doesn't change the
#    type node, so its dependents need no re-resolve).
_PROVENANCE_RELS = ("DEFINES",)
_LATERAL_RELS = ("CALLS", "DEPENDS_ON", "EMBEDS", "DEFINED_IN")
_CONTAINMENT_RELS = ("HAS_MODULE", "HAS_PACKAGE", "HAS_FILE", "HAS_TYPE", "HAS_FUNCTION",
                     "HAS_METHOD", "HAS_FIELD", "HAS_PARAMETER", "HAS_CONST")
_SYMBOL_REF_RELS = ("CALLS", "DEPENDS_ON", "EMBEDS")
_TARGETABLE = ("Function", "Method", "Struct", "Interface", "TypeAlias", "Const")


def _scope_key(path: str) -> str:
    """A file's stable scope key = normalized path -- exactly what `File.path`/`file_id` use."""
    return path.replace("\\", "/").lstrip("./")


def _go_project_node(project_name, repo_uri, proj_id, target_sha, ref) -> GraphNode:
    return GraphNode("Project", {"id": proj_id},
                     {"name": project_name, "repo_uri": repo_uri, "commit_sha": target_sha, "ref": ref})


def _go_referenced_names(rf) -> set[str]:
    """Bare/qualified names an emit file references cross-file (call names + type refs). Only used to
    bound the seed; read_go_index seeds the whole non-emit index, so over-collecting is harmless."""
    names: set[str] = set()
    for fn in rf.functions:
        names.update(c.name for c in fn.calls)
        names.update(fn.type_refs)
    for t in rf.types:
        names.update(t.embeds)
        for fld in t.fields:
            names.update(fld.type_refs)
    return {n for n in names if n}


def _go_defined_symbol_ids(raw, proj_id: str) -> list[tuple[str, str]]:
    """(node_id, label) of the program-global symbols this file defines, via the SAME id functions
    the emitter uses -- drives v2 reverse-dep narrowing (only added/removed ids re-resolve callers).
    Functions/types/consts are computed precisely (owner-independent). METHODS are omitted: a
    method's id depends on its receiver type's id, which may live in another (non-emit) file; omitting
    them just makes methods conservatively 'always changed' (their callers are pulled) -- correct,
    only un-narrowed. A mismatch would at worst over-collect (safe), never miss a removal.

    The package id is keyed by the file's DIRECTORY (pure path, no go.mod needed) -- plus the
    external-test-package discriminator (`_pkg_key`) -- the same key the emitter uses, so these ids
    match the emitted ones without the module map this callback lacks."""
    pid = ids.package_id(proj_id, resolution._pkg_key(resolution._dir_of(raw.path), raw.package))
    out: list[tuple[str, str]] = []
    for fn in raw.functions:
        if fn.receiver is None:
            out.append((ids.function_id(pid, fn.name, [p.type for p in fn.parameters]), "Function"))
    for t in raw.types:
        out.append((ids.type_id(pid, t.kind, t.name, t.type_parameters), t.kind))
    for c in raw.consts:
        out.append((ids.const_id(pid, c.name), "Const"))
    return out


def read_go_index(writer: GraphWriter, proj_id: str, exclude_keys: set[str], names: set[str]) -> resolution.GoGraphIndex:
    """Reconstruct Go's resolution tables from the graph for every file EXCEPT `exclude_keys` (the
    emit + removed set), so `resolve_incremental` resolves an emit file's cross-package refs without
    re-parsing the project. Types come back as lightweight stubs (id/kind/name/package + fields, the
    last so an emit method on a non-emit receiver can still resolve field-typed calls). Every query
    is an id-index seek."""
    idx = resolution.GoGraphIndex()
    lo, hi = proj_id + "/", proj_id + "/" + chr(0xFFFF)
    exclude_file_ids = {ids.file_id(proj_id, k) for k in exclude_keys}
    seed_file_ids = [
        str(fid) for (fid,) in writer.ro_query(
            "MATCH (f:File) WHERE f.id >= $lo AND f.id < $hi RETURN f.id", {"lo": lo, "hi": hi}
        ).result_set if str(fid) not in exclude_file_ids
    ]

    # The go.mod module map (module_dir -> module_path), so an emit file's import path is computed
    # exactly as a full ingest would. Current because git_sync forces a full recompute on any go.mod
    # structural change (so the graph's Module nodes never lag the manifests during an incremental).
    idx.module_map = {
        str(mdir): str(mpath) for mdir, mpath in writer.ro_query(
            "MATCH (m:Module) WHERE m.id >= $lo AND m.id < $hi RETURN m.dir, m.name", {"lo": lo, "hi": hi}
        ).result_set
    }
    # Every project package's import path (a package spans files, so this is the full match-key set).
    idx.project_import_paths = {
        str(ip) for (ip,) in writer.ro_query(
            "MATCH (p:Package) WHERE p.id >= $lo AND p.id < $hi RETURN p.import_path", {"lo": lo, "hi": hi}
        ).result_set if ip is not None
    }
    if not seed_file_ids:
        return idx

    # Named types defined by the surviving files -> lightweight (import_path, name) -> _Type stub.
    fields_by_tid: dict[str, list] = defaultdict(list)
    for tid, fname, ftype in writer.ro_query(
        "MATCH (t)-[:HAS_FIELD]->(fld:Field) WHERE (t:Struct OR t:Interface OR t:TypeAlias) "
        "AND t.id >= $lo AND t.id < $hi RETURN t.id, fld.name, fld.type", {"lo": lo, "hi": hi},
    ).result_set:
        fields_by_tid[str(tid)].append(RawField(name=str(fname), type=str(ftype) if ftype is not None else ""))
    for tid, label, tname, pname, ip in writer.ro_query(
        f"MATCH (file:File)-[:DEFINES]->(t), (pk:Package)-[:HAS_TYPE]->(t) "
        f"WHERE file.id IN $fids AND (t:Struct OR t:Interface OR t:TypeAlias) "
        f"RETURN DISTINCT t.id, {semantic_label('t')}, t.name, pk.name, pk.import_path", {"fids": seed_file_ids},
    ).result_set:
        stub = resolution.make_type_stub(str(tid), str(label), str(tname), "", str(pname), str(ip),
                                         fields_by_tid.get(str(tid), []))
        idx.type_by_import_path.setdefault((str(ip), str(tname)), stub)

    # Free functions defined by the surviving files -> name -> [(func_id, import_path)].
    for fname, fid, ip in writer.ro_query(
        "MATCH (file:File)-[:DEFINES]->(f:Function), (pk:Package)-[:HAS_FUNCTION]->(f) "
        "WHERE file.id IN $fids RETURN DISTINCT f.name, f.id, pk.import_path", {"fids": seed_file_ids},
    ).result_set:
        idx.func_by_name.setdefault(str(fname), []).append((str(fid), str(ip)))

    # Methods (incl. interface specs) defined by the surviving files -> name -> [(owner_id, method_id)].
    for mname, owner_id, mid in writer.ro_query(
        "MATCH (file:File)-[:DEFINES]->(m:Method), (owner)-[:HAS_METHOD]->(m) "
        "WHERE file.id IN $fids RETURN DISTINCT m.name, owner.id, m.id", {"fids": seed_file_ids},
    ).result_set:
        idx.method_by_name.setdefault(str(mname), []).append((str(owner_id), str(mid)))
    return idx


GO_SPEC = ScopedSpec(
    language="go",
    node_schemas=tuple(NODE_SCHEMAS),
    node_labels=tuple(s.label for s in NODE_SCHEMAS),
    file_suffixes=tuple(_FILE_SUFFIXES),
    excluded_dir_names=frozenset(_EXCLUDED_DIR_NAMES),
    symbol_ref_rels=_SYMBOL_REF_RELS,
    lateral_rels=_LATERAL_RELS,
    containment_rels=_CONTAINMENT_RELS,
    targetable_labels=_TARGETABLE,
    import_rel=None,  # Go has no IMPORTS edge; cross-package unresolved calls are extern:// placeholders
    import_target_labels=(),
    project_id=ids.project_id,
    scope_id_for_path=ids.file_id,
    scope_key_for_path=_scope_key,
    parse=parse_source,
    is_broken=lambda rf: False,  # tree-sitter best-effort; identical in both engines (matches C/Bash)
    referenced_names=_go_referenced_names,
    read_index=read_go_index,
    resolve_incremental=resolution.resolve_incremental,
    project_node=_go_project_node,
    # Left None (unlike before): a cross-repo linkage key is now the IMPORT-PATH-qualified
    # `extern://go/<import-path>.<Name>`, which needs the go.mod module path a raw .go file alone
    # can't supply -- so the in-repo placeholder-satisfaction closure (which only has the raws) can't
    # reconstruct it. Correctness is unaffected: cross-repo re-binding still works via the standard
    # reconcile/demote path (ADR 0004, reconcile_linkages), and every corpus transition edits a caller
    # alongside its callee, so the differential oracle stays exact. (Mirrors the Rust ingestor.)
    satisfied_linkage_keys=None,
    provenance_rels=_PROVENANCE_RELS,
    definition_rels=("DEFINES",),  # no headers -> no declaration/definition split
    linkable_labels=("Function", "Method"),
    defined_symbol_ids=_go_defined_symbol_ids,
    # Module (go.mod) and Package (dir) are aggregation tiers co-authored by every member file; their
    # props are id-derived, so they're excluded from the co-DEFINER winner closure (editing one file
    # must not pull its siblings). Both stay provenance-owned (File-DEFINES-*) for refcount GC + in the
    # co-contributor removal closure.
    aggregation_labels=("Package", "Module"),
    # A Go method `func (T) M()` can live in any file of T's package, not T's declaring file. When T is
    # touched, pull its method-definer files so T's whole method set is re-authored together; else
    # resyncing a file that merely references T deletes T's methods defined in sibling files (the
    # authored-engine cross-file-member data loss -- same class as Rust `impl` blocks).
    type_member_rels=("HAS_METHOD",),
)


def incremental_go_sync(writer, source, target_sha, changes, *, project_name, repo_uri, ref=None):
    """Thin wrapper over the shared authored-shared engine (git_sync routes through the same one)."""
    return incremental_sync_authored(writer, GO_SPEC, source, target_sha, changes,
                                     project_name=project_name, repo_uri=repo_uri, ref=ref)
