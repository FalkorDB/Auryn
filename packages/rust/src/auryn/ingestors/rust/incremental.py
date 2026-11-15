"""Rust binding for the AUTHORED-SHARED incremental resync engine (core/incremental_scoped.py,
ADR 0005) -- the fourth language on it (after C, Go, Java), validating again that the engine + its
fixes (definition ownership, order-independent multi-definer winner, v2 blast-radius narrowing)
carry over with only per-language glue and NO core changes.

Rust is authored-shared like Go/Java but with a hierarchical MODULE tier: a Struct/Enum/Trait/
Function/Method/Const is keyed under its containing module (`rust://<proj>/module/<dotted>...`) or,
for a crate-root item, directly under the project id -- NOT under a file. Many files feed the
crate-root scope (and each `mod`), and a type's methods live in separate `impl` blocks that may sit
in a different file than the struct. So the per-file ownership handle is the `File-[:DEFINES]->entity`
edge (added in resolution.py, uniform with Go/C): a struct's DEFINES comes from its declaring file,
an impl-block method's from the impl's file. Rust has no forward declarations, so there is no
declaration/definition split -- definition_rels == provenance_rels == ("DEFINES",). It shares C's/Go's
multi-definition collision (a multi-binary crate's several crate-root `fn main`), handled by the same
file-path sort + co-definer closure + keep-first node dedup.

Supplies the two language-specific pieces -- reconstructing Rust's resolution tables from the graph
(`read_rust_index`) and emitting a subset against them (`resolution.resolve_incremental`) -- plus the
declarative spec. Correctness (incremental == full) is enforced by tests/sync/test_delta_differential
(single repo) and tests/sync/test_delta_cross_repo (bidirectional cross-repo).

Note on `satisfied_linkage_keys` (left None, unlike C/Go): Rust's fast resolver DROPS an unresolved
bare/name-based call rather than materializing a placeholder, and a cross-module/crate placeholder's
id embeds the caller-written path (`extern://rust/a::b::f`) which a definer can't predict -- so the
linkage-key placeholder-satisfaction closure doesn't fit. Cross-repo re-binding still works via the
standard reconcile/demote path (ADR 0004). The only uncovered case is an *untouched* caller whose
name-based call would newly resolve to a like-named callee added in another file this sync -- the
deep rust-analyzer pass's job; every corpus transition edits the caller alongside its callee (as a
full recompute would see it), so the differential oracle stays exact.
"""

from __future__ import annotations

from collections import defaultdict

from auryn.core.graph_client import GraphWriter, semantic_label
from auryn.core.graph_model import GraphNode
from auryn.core.incremental_scoped import ChangeSet, ScopedSpec, incremental_sync_authored
from auryn.ingestors.rust import cargo, ids, resolution
from auryn.ingestors.rust.file_parser import parse_source
from auryn.ingestors.rust.plugin import EXCLUDED_DIR_NAMES as _EXCLUDED_DIR_NAMES
from auryn.ingestors.rust.plugin import FILE_SUFFIXES as _FILE_SUFFIXES
from auryn.ingestors.rust.raw_model import RawField
from auryn.ingestors.rust.schema import NODE_SCHEMAS

__all__ = ["ChangeSet", "RUST_SPEC", "read_rust_index", "incremental_rust_sync"]

# Edge classification (see core/incremental_scoped.read_authored_fingerprints):
#  - provenance (File→entity ownership handle): DEFINES.
#  - lateral (source-owned): CALLS + DEPENDS_ON + IMPLEMENTS + EXTENDS + DEFINED_IN (all sourced at a
#    func/method/type/field node, all re-emitted when that node's file re-emits). DEFINED_IN (type->its
#    File) is source-owned by the type: when the type MOVES to another file its old DEFINED_IN must be
#    diffed away, so it has to be read back with the type -- else the stale edge survives (incremental != full).
#  - containment (target-owned): the structural spine, incl. the Module tier + HAS_FILE.
#  - symbol-ref (reverse-dep closure): a change to a type/func/method/trait affects its callers,
#    type-dependents, implementers and sub-traits -> CALLS + DEPENDS_ON + IMPLEMENTS + EXTENDS (NOT
#    DEFINED_IN: moving a type's file doesn't change the type node, so dependents need no re-resolve).
_PROVENANCE_RELS = ("DEFINES",)
_LATERAL_RELS = ("CALLS", "DEPENDS_ON", "IMPLEMENTS", "EXTENDS", "DEFINED_IN")
_CONTAINMENT_RELS = ("HAS_PACKAGE", "HAS_TARGET", "HAS_MODULE", "HAS_SUBMODULE", "HAS_FILE",
                     "HAS_TYPE", "HAS_FUNCTION", "HAS_METHOD", "HAS_FIELD", "HAS_VARIANT",
                     "HAS_PARAMETER", "HAS_CONST")
_SYMBOL_REF_RELS = ("CALLS", "DEPENDS_ON", "IMPLEMENTS", "EXTENDS")
_TARGETABLE = ("Struct", "Enum", "Trait", "Function", "Method")


def _scope_key(path: str) -> str:
    """A file's stable scope key = normalized path -- exactly what `File.path`/`file_id` use."""
    return path.replace("\\", "/").lstrip("./")


def _rust_project_node(project_name, repo_uri, proj_id, target_sha, ref) -> GraphNode:
    return GraphNode("Project", {"id": proj_id},
                     {"name": project_name, "repo_uri": repo_uri, "commit_sha": target_sha, "ref": ref})


def _type_names(text: str | None) -> set[str]:
    return set(resolution._referenced_names(text)) if text else set()


def _rust_referenced_names(rf) -> set[str]:
    """Bare/qualified names an emit file references cross-file (call names + type refs). Only used to
    bound the seed; read_rust_index seeds the whole non-emit index, so over-collecting is harmless."""
    names: set[str] = set()

    def walk(scope) -> None:
        for fn in getattr(scope, "functions", []):
            names.update(c.name for c in fn.calls)
            names.update(_type_names(fn.return_type))
            for p in fn.parameters:
                names.update(_type_names(p.type))
        for rt in getattr(scope, "types", []):
            names.update(rt.supertraits)
            for f in rt.fields:
                names.update(_type_names(f.type))
            for m in rt.trait_methods:
                names.update(c.name for c in m.calls)
        for impl in getattr(scope, "impls", []):
            names.add(impl.type_name)
            if impl.trait_name:
                names.add(impl.trait_name)
            for m in impl.methods:
                names.update(c.name for c in m.calls)
                names.update(_type_names(m.return_type))
                for p in m.parameters:
                    names.update(_type_names(p.type))
        for m in getattr(scope, "modules", []):
            walk(m)

    walk(rf)
    return {n for n in names if n}


def _rust_satisfied_linkage_keys(raws) -> set[str]:  # unused (spec sets satisfied_linkage_keys=None)
    return set()


def _rust_defined_symbol_ids(raw, proj_id: str) -> list[tuple[str, str]]:
    """(node_id, label) of the program-global symbols this file defines, via the SAME id functions
    the emitter uses -- drives v2 reverse-dep narrowing (only added/removed ids re-resolve callers).
    Types/functions/consts are computed precisely (scope is the containing module or the project,
    both file-independent). METHODS are omitted: a method's id depends on its receiver type's id,
    whose scope/arity may live in another (non-emit) file (an `impl` split from its `struct`);
    omitting them just makes methods conservatively 'always changed' (their callers are pulled) --
    correct, only un-narrowed. A mismatch would at worst over-collect (safe), never miss a removal."""
    out: list[tuple[str, str]] = []

    def walk(scope, scope_id: str, root_id: str, dotted: str) -> None:
        for rt in getattr(scope, "types", []):
            out.append((ids.type_id(scope_id, rt.kind, rt.name, rt.type_parameters), rt.kind))
        for fn in getattr(scope, "functions", []):
            out.append((ids.function_id(scope_id, fn.name, [p.type for p in fn.parameters]), "Function"))
        for rc in getattr(scope, "consts", []):
            out.append((ids.const_id(scope_id, rc.name), "Const"))
        for m in getattr(scope, "modules", []):
            child = f"{dotted}.{m.name}" if dotted else m.name
            walk(m, ids.module_id(root_id, child), root_id, child)

    # The file's scope root is its crate (issue #5), keyed from the path alone -- identical to what
    # resolution._resolve computes, so these ids match the emitted ones (the v2 narrowing's premise).
    ct = cargo.crate_target_for_path(raw.path)
    cid = ids.crate_id(proj_id, ct.package_dir, ct.kind, ct.name)
    walk(raw, cid, cid, "")
    return out


def read_rust_index(writer: GraphWriter, proj_id: str, exclude_keys: set[str], names: set[str]) -> resolution.RustGraphIndex:
    """Reconstruct Rust's resolution tables from the graph for every file EXCEPT `exclude_keys` (the
    emit + removed set), so `resolve_incremental` resolves an emit file's cross-module/crate refs
    without re-parsing the project. Types come back as lightweight stubs (id/kind/name + fields, the
    last so an emit `impl` method on a non-emit receiver can still resolve field-typed calls). Every
    query is an id-index seek (File range) or an id-IN seek over the surviving file set."""
    idx = resolution.RustGraphIndex()
    lo, hi = proj_id + "/", proj_id + "/" + chr(0xFFFF)
    exclude_file_ids = {ids.file_id(proj_id, k) for k in exclude_keys}
    seed_file_ids = [
        str(fid) for (fid,) in writer.ro_query(
            "MATCH (f:File) WHERE f.id >= $lo AND f.id < $hi RETURN f.id", {"lo": lo, "hi": hi}
        ).result_set if str(fid) not in exclude_file_ids
    ]
    if not seed_file_ids:
        return idx

    # Named types (Struct/Enum/Trait) defined by the surviving files -> _TypeEntry stubs, with a
    # struct's fields attached (so an emit method on a non-emit receiver types field calls).
    fields_by_tid: dict[str, list] = defaultdict(list)
    for tid, fname, ftype in writer.ro_query(
        "MATCH (t)-[:HAS_FIELD]->(fld:Field) WHERE (t:Struct OR t:Enum OR t:Trait) "
        "AND t.id >= $lo AND t.id < $hi RETURN t.id, fld.name, fld.type", {"lo": lo, "hi": hi},
    ).result_set:
        fields_by_tid[str(tid)].append(RawField(name=str(fname), type=str(ftype) if ftype is not None else ""))
    for tid, label, tname in writer.ro_query(
        f"MATCH (file:File)-[:DEFINES]->(t) WHERE file.id IN $fids AND (t:Struct OR t:Enum OR t:Trait) "
        f"RETURN DISTINCT t.id, {semantic_label('t')}, t.name", {"fids": seed_file_ids},
    ).result_set:
        idx.types.append(resolution.make_type_stub(str(tid), str(label), str(tname), fields_by_tid.get(str(tid), [])))

    # Free functions defined by the surviving files -> name -> [(func_id, scope_id)] (scope = the
    # HAS_FUNCTION source: a Module or the Project).
    for fname, fid, sid in writer.ro_query(
        "MATCH (file:File)-[:DEFINES]->(f:Function), (scope)-[:HAS_FUNCTION]->(f) "
        "WHERE file.id IN $fids RETURN DISTINCT f.name, f.id, scope.id", {"fids": seed_file_ids},
    ).result_set:
        idx.func_by_name.setdefault(str(fname), []).append((str(fid), str(sid)))

    # Methods (incl. trait-declared) defined by the surviving files -> name -> [(owner_id, mid, owner_name)].
    for mname, owner_id, mid, owner_name in writer.ro_query(
        "MATCH (file:File)-[:DEFINES]->(m:Method), (owner)-[:HAS_METHOD]->(m) "
        "WHERE file.id IN $fids RETURN DISTINCT m.name, owner.id, m.id, owner.name", {"fids": seed_file_ids},
    ).result_set:
        idx.method_by_name.setdefault(str(mname), []).append((str(owner_id), str(mid), str(owner_name)))

    # Project MODULE segments declared by surviving files, so the `_external_path` guard recognizes an
    # intra-project module declared outside the emit set (a `mod x` in a non-emit file) and drops `x::f()`
    # instead of materializing a stale extern placeholder the full path never creates (incremental != full).
    for (mname,) in writer.ro_query(
        "MATCH (file:File)-[:DEFINES]->(m:Module) WHERE file.id IN $fids RETURN DISTINCT m.name",
        {"fids": seed_file_ids},
    ).result_set:
        if mname is not None:
            idx.project_module_names.add(str(mname))
    return idx


RUST_SPEC = ScopedSpec(
    language="rust",
    node_schemas=tuple(NODE_SCHEMAS),
    node_labels=tuple(s.label for s in NODE_SCHEMAS),
    file_suffixes=tuple(_FILE_SUFFIXES),
    excluded_dir_names=frozenset(_EXCLUDED_DIR_NAMES),
    symbol_ref_rels=_SYMBOL_REF_RELS,
    lateral_rels=_LATERAL_RELS,
    containment_rels=_CONTAINMENT_RELS,
    targetable_labels=_TARGETABLE,
    import_rel=None,  # Rust `use` paths are deep-pass fodder; cross-module/crate misses are extern:// placeholders
    import_target_labels=(),
    project_id=ids.project_id,
    scope_id_for_path=ids.file_id,
    scope_key_for_path=_scope_key,
    parse=parse_source,
    is_broken=lambda rf: False,  # tree-sitter best-effort; identical in both engines (matches C/Go/Bash)
    referenced_names=_rust_referenced_names,
    read_index=read_rust_index,
    resolve_incremental=resolution.resolve_incremental,
    project_node=_rust_project_node,
    satisfied_linkage_keys=None,  # see module docstring: Rust drops unresolved bare calls (no placeholder)
    provenance_rels=_PROVENANCE_RELS,
    definition_rels=("DEFINES",),  # no forward declarations -> no declaration/definition split
    linkable_labels=("Function", "Method"),
    defined_symbol_ids=_rust_defined_symbol_ids,
    # Aggregation tiers conflate many files into one shared node: a Module (every file's inline
    # `mod tests {}`), and (issue #5) a Crate / Package (every file in a crate / package authors the
    # same node). Excluding them from the co-DEFINER winner closure keeps a one-file edit from
    # dragging the whole cluster in. Safe: all three carry only id-derived props (name/kind follow
    # from the id -- manifest facts are layered on by git_sync's reconcile, not by the delta engine).
    # GC still works via touched_defined + the co-contributor (removal) closure + the refcount sweep.
    aggregation_labels=("Module", "Crate", "Package"),
    # A type's methods live in `impl` blocks that may sit in a different file (or crate) than the
    # struct/enum/trait -- including `impl Trait for Type` (the IMPLEMENTS edge) and `impl From<X> for
    # OtherCrate::Type` (an orphan impl keyed under the foreign type). So when a type is touched, pull
    # its method-definer files via HAS_METHOD, re-authoring the type's full method set + IMPLEMENTS
    # edges together (else resyncing a file that merely references the type deletes those cross-file
    # methods/edges -- the real-repo data loss this closes). HAS_FIELD/HAS_VARIANT/HAS_CONST are
    # single-file (declared with the type) so they're not needed here.
    type_member_rels=("HAS_METHOD",),
    # A METHODLESS `impl Trait for Type {}` carries an IMPLEMENTS edge but no method node, and the impl
    # can sit in the trait's file or any crate file (the orphan rule) -- so no co-member breadcrumb reaches
    # its authoring file. IMPLEMENTS edges are stamped with their `_src_file` (resolution.py), and the
    # authored read-back excludes any whose `_src_file` is outside the emit set, keeping a transitively-
    # pulled type's cross-file impl edge in place instead of deleting it (see ScopedSpec.type_impl_rels).
    # EXTENDS is authored at the subtrait's own declaration (always re-authored with the source), so it
    # isn't listed here.
    type_impl_rels=("IMPLEMENTS",),
)


def incremental_rust_sync(writer, source, target_sha, changes, *, project_name, repo_uri, ref=None):
    """Thin wrapper over the shared authored-shared engine (git_sync routes through the same one)."""
    return incremental_sync_authored(writer, RUST_SPEC, source, target_sha, changes,
                                     project_name=project_name, repo_uri=repo_uri, ref=ref)
