"""C binding for the AUTHORED-SHARED incremental resync engine (core/incremental_scoped.py,
ADR 0005).

C is the archetypal authored-shared language: almost every entity is program-global (a function,
struct, union, enum, typedef, extern global is keyed `c://<proj>/<kind>/<name>`, NOT under a file),
shared across the header(s) that DECLARE it and the .c that DEFINES it; only statics/macros are
file-scoped. So the single per-file handle is the `File-[:DEFINES|DECLARES]->entity` edge, and this
module opts the engine into its provenance-scoped path via `C_SPEC.provenance_rels`.

It supplies the two genuinely language-specific pieces -- reconstructing C's resolution symbol
tables from the graph (`read_c_index`) and emitting a subset against them
(`resolution.resolve_incremental`) -- plus the small declarative glue (edge classification,
id<->scope mapping, the cross-repo linkable label). Correctness (incremental == full recompute,
node/edge-identical, across standalone / multi-repo / cross-repo-bidirectional scenarios) is
enforced by the differential oracle in tests/sync/test_delta_differential.py and
tests/sync/test_delta_cross_repo.py.
"""

from __future__ import annotations

import posixpath

from auryn.core.graph_client import SHARED_NODE_LABEL, GraphWriter, semantic_label
from auryn.core.graph_model import GraphNode
from auryn.core.cpp_headers import HEADER_SUFFIX, is_cpp_header_blob
from auryn.core.incremental_scoped import ChangeSet, ScopedSpec, incremental_sync_authored
from auryn.ingestors.c import ids, resolution
from auryn.ingestors.c.file_parser import parse_source
from auryn.ingestors.c.plugin import EXCLUDED_DIR_NAMES as _EXCLUDED_DIR_NAMES
from auryn.ingestors.c.plugin import FILE_SUFFIXES as _FILE_SUFFIXES
from auryn.ingestors.c.schema import NODE_SCHEMAS

__all__ = ["ChangeSet", "C_SPEC", "read_c_index", "incremental_c_sync"]

# Edge classification for the engine (see core/incremental_scoped.read_authored_fingerprints):
#  - provenance (File→entity, the per-file ownership handle): DEFINES / DECLARES.
#  - lateral (source-owned): CALLS + REFERENCES (Function/Field→type) + INCLUDES (File→File).
#  - containment (target-owned): a program-global node's children (Fields, Parameters).
#  - symbol-ref (walked for the reverse-dep closure): CALLS + REFERENCES.
_PROVENANCE_RELS = ("DEFINES", "DECLARES")
_LATERAL_RELS = ("CALLS", "REFERENCES", "INCLUDES")
_CONTAINMENT_RELS = ("HAS_FIELD", "HAS_PARAMETER")
_SYMBOL_REF_RELS = ("CALLS", "REFERENCES")


def _scope_key(path: str) -> str:
    """A file's stable scope key = its normalized path -- the exact string `File.path` stores and
    `file_id` is built from, so it agrees with the id-prefix the engine scopes file-local nodes by."""
    return path.replace("\\", "/").lstrip("./")


def _c_project_node(project_name, repo_uri, proj_id, target_sha, ref) -> GraphNode:
    return GraphNode(
        "Project", {"id": proj_id},
        {"name": project_name, "repo_uri": repo_uri, "commit_sha": target_sha, "ref": ref},
    )


def _c_referenced_names(rf) -> set[str]:
    """Bare names an emit file could resolve cross-file: called function names + referenced type
    names. Used to bound the index seed; over-collecting is safe (the oracle catches an under)."""
    names: set[str] = set()
    for fn in rf.functions:
        names.update(fn.calls)
        names.add(resolution._bare(fn.return_type))
        for p in fn.params:
            names.add(resolution._bare(p.type))
    for rec in rf.records:
        for f in rec.fields:
            if f.type_ref:
                names.add(f.type_ref)
    return {n for n in names if n}


def _c_defined_symbol_ids(raw, proj_id: str) -> list[tuple[str, str]]:
    """(node_id, label) of every program-global symbol this file DEFINES or DECLARES, via the SAME
    id functions the emitter uses (single source of truth). Drives the v2 reverse-dep narrowing:
    only names whose id is added/removed vs the graph re-resolve their callers. Statics/macros are
    file-local (no cross-file callers), so they're omitted."""
    fid = ids.file_id(proj_id, raw.path)
    out: list[tuple[str, str]] = []
    for fn in raw.functions:
        if not fn.is_static:
            out.append((resolution._func_id(fn, fid, proj_id), "Function"))
    for rec in raw.records:
        out.append((resolution._record_id(rec, proj_id), "Struct" if rec.kind == "struct" else "Union"))
    for en in raw.enums:
        out.append((ids.enum_id(proj_id, en.name), "Enum"))
    for td in raw.typedefs:
        out.append((ids.typedef_id(proj_id, td.name), "TypeAlias"))
    for g in raw.globals:
        if not g.is_static:
            out.append((ids.global_id(proj_id, g.name), "GlobalVariable"))
    return out


def _c_claims_path(path: str, read_blob) -> bool:
    """C owns a `.c`/`.h`, EXCEPT a `.h` whose content is C++ -- that belongs to the C++ ingestor (the C
    grammar would mangle a class-in-header; see core.cpp_headers). Mirrors the full-ingest walk in
    git_sync, so a header's owner is identical on a full ingest and an incremental resync."""
    if path.endswith(HEADER_SUFFIX):
        return not is_cpp_header_blob(read_blob())
    return True


def _c_satisfied_linkage_keys(raws) -> set[str]:
    """The extern:// linkages the emit set's external definitions can satisfy: an untouched caller
    pointing at `extern://c/<name>` should be re-resolved onto a just-defined external function of
    that name. Drives the engine's placeholder-satisfaction closure (bare-name resolution)."""
    return {ids.extern_function_key(fn.name) for rf in raws for fn in rf.functions if not fn.is_static}


def read_c_index(writer: GraphWriter, proj_id: str, exclude_keys: set[str], names: set[str]) -> resolution.CGraphIndex:
    """Reconstruct C's resolution symbol tables from the graph for every file EXCEPT `exclude_keys`
    (the emit + removed set), so `resolve_incremental` resolves an emit file's cross-file references
    without re-parsing the project. Seeds are drawn along `DEFINES`/`DECLARES` from the surviving
    files only -- so a symbol defined ONLY by an emit/removed file is NOT seeded (it is recomputed
    from the fresh parse, and vanishes if the edit dropped it). Every query is an id-index seek."""
    idx = resolution.CGraphIndex()
    lo, hi = proj_id + "/", proj_id + "/" + chr(0xFFFF)
    exclude_file_ids = {ids.file_id(proj_id, k) for k in exclude_keys}

    # File paths for #include resolution (normalized path + unique basename -> File id).
    full: dict[str, str] = {}
    basename_count: dict[str, int] = {}
    seed_file_ids: list[str] = []
    for fid, path in writer.ro_query(
        "MATCH (f:File) WHERE f.id >= $lo AND f.id < $hi RETURN f.id, f.path", {"lo": lo, "hi": hi}
    ).result_set:
        if fid in exclude_file_ids:
            continue
        seed_file_ids.append(str(fid))
        norm = resolution._norm(path)
        full[norm] = str(fid)
        b = posixpath.basename(norm)
        basename_count[b] = basename_count.get(b, 0) + 1
    idx.include_index = dict(full)
    for norm, fid in full.items():
        b = posixpath.basename(norm)
        if basename_count[b] == 1:
            idx.include_index.setdefault(b, fid)

    if not seed_file_ids:
        return idx

    # External functions declared/defined by the surviving files (name -> program-global id).
    for name, fnid in writer.ro_query(
        "MATCH (file:File)-[:DEFINES|DECLARES]->(fn:Function) "
        "WHERE file.id IN $fids AND fn.linkage = 'external' RETURN DISTINCT fn.name, fn.id",
        {"fids": seed_file_ids},
    ).result_set:
        idx.extern_funcs.setdefault(str(name), str(fnid))

    # Named types declared/defined by the surviving files (with C's tag/typedef precedence).
    for name, label, tid in writer.ro_query(
        f"MATCH (file:File)-[:DEFINES|DECLARES]->(t) WHERE file.id IN $fids "
        f"AND (t:Struct OR t:Union OR t:Enum OR t:TypeAlias) RETURN t.name, {semantic_label('t')}, t.id",
        {"fids": seed_file_ids},
    ).result_set:
        cur = idx.type_index.get(str(name))
        if cur is None or resolution._TYPE_PRECEDENCE[str(label)] > resolution._TYPE_PRECEDENCE[cur[0]]:
            idx.type_index[str(name)] = (str(label), str(tid))

    # Names DEFINED (not merely declared) by the surviving files: resolve_incremental emits only a
    # DECLARES edge (never a sparse node) for a prototype/forward-decl of one of these that appears
    # in the emit set, so the untouched definition's node is never overwritten (ADR 0005).
    for (name,) in writer.ro_query(
        "MATCH (file:File)-[:DEFINES]->(fn:Function) WHERE file.id IN $fids AND fn.linkage = 'external' "
        "RETURN DISTINCT fn.name", {"fids": seed_file_ids},
    ).result_set:
        idx.defined_func_names.add(str(name))
    for (name,) in writer.ro_query(
        "MATCH (file:File)-[:DEFINES]->(t) WHERE file.id IN $fids "
        "AND (t:Struct OR t:Union OR t:Enum OR t:TypeAlias) RETURN DISTINCT t.name", {"fids": seed_file_ids},
    ).result_set:
        idx.defined_type_names.add(str(name))
    return idx


C_SPEC = ScopedSpec(
    language="c",
    node_schemas=tuple(NODE_SCHEMAS),
    node_labels=tuple(s.label for s in NODE_SCHEMAS),
    file_suffixes=tuple(_FILE_SUFFIXES),
    excluded_dir_names=frozenset(_EXCLUDED_DIR_NAMES),
    symbol_ref_rels=_SYMBOL_REF_RELS,
    lateral_rels=_LATERAL_RELS,
    containment_rels=_CONTAINMENT_RELS,
    targetable_labels=("Function", "Struct", "Union", "Enum", "TypeAlias", "GlobalVariable"),
    import_rel="INCLUDES",
    import_target_labels=("File",),
    project_id=ids.project_id,
    scope_id_for_path=ids.file_id,
    scope_key_for_path=_scope_key,
    parse=parse_source,
    # tree-sitter-c recovers from errors (best-effort partial), identical in both engines -> accept
    # it, so incremental == full recompute even mid-edit (matches Bash).
    is_broken=lambda rf: False,
    referenced_names=_c_referenced_names,
    read_index=read_c_index,
    resolve_incremental=resolution.resolve_incremental,
    project_node=_c_project_node,
    satisfied_linkage_keys=_c_satisfied_linkage_keys,
    provenance_rels=_PROVENANCE_RELS,
    definition_rels=("DEFINES",),  # DECLARES owns only the declares edge, not the symbol's def data
    linkable_labels=("Function",),
    defined_symbol_ids=_c_defined_symbol_ids,  # v2 blast-radius narrowing
    claims_path=_c_claims_path,  # `.h` with C++ content belongs to the C++ ingestor, not C
)


def incremental_c_sync(writer, source, target_sha, changes, *, project_name, repo_uri, ref=None):
    """Thin wrapper over the shared authored-shared engine, used by the equivalence oracle (the
    git_sync routing goes through core.route_incremental -> the same engine)."""
    return incremental_sync_authored(writer, C_SPEC, source, target_sha, changes,
                                     project_name=project_name, repo_uri=repo_uri, ref=ref)
