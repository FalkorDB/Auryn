"""TypeScript/JavaScript binding for the generic incremental resync engine
(core/incremental_scoped.py, ADR 0002).

TS is the closest non-Python match to the engine's model: a module IS a file (path-scoped
ids), so every entity nests under its file's `module_id` and the `IMPORTS` (Module->Module)
edge is the reverse-dependency breadcrumb. This module supplies only the TS-specific pieces
via `TS_SPEC`: id↔scope mapping, edge classification, and the two language-specific functions
-- reconstructing the resolution symbol table from the graph (`read_graph_index`) and emitting
a subset against it (`resolution.resolve_incremental`).

Correctness (== a full recompute) is enforced by
tests/ingestors/test_typescript_incremental_equivalence.py.
"""

from __future__ import annotations

import posixpath

from auryn.core.graph_client import GraphWriter
from auryn.core.graph_model import GraphNode
from auryn.core.incremental_scoped import ChangeSet, ScopedSpec, incremental_sync
from auryn.ingestors.typescript import ids, resolution
from auryn.ingestors.typescript.file_parser import parse_source
from auryn.ingestors.typescript.plugin import EXCLUDED_DIR_NAMES as _EXCLUDED_DIR_NAMES
from auryn.ingestors.typescript.plugin import FILE_SUFFIXES as _FILE_SUFFIXES
from auryn.ingestors.typescript.schema import NODE_SCHEMAS

__all__ = ["ChangeSet", "TS_SPEC", "read_graph_index"]

_CONTAINMENT_RELS = (
    "HAS_MODULE", "HAS_CLASS", "HAS_INTERFACE", "HAS_ENUM", "HAS_TYPE_ALIAS",
    "HAS_FUNCTION", "HAS_METHOD", "HAS_PROPERTY", "HAS_PARAMETER", "HAS_ENUM_MEMBER",
)
_SYMBOL_REF_RELS = ("CALLS", "EXTENDS", "IMPLEMENTS", "RETURNS", "REFERENCES")


def _range(prefix: str) -> tuple[str, str]:
    return prefix, prefix + chr(0xFFFF)


def _scope_key(path: str) -> str:
    """A file's stable scope key = its normalized path (what `Module.path` stores and
    `module_id` is built from), so it agrees with the id-prefix the engine scopes by."""
    return path.replace("\\", "/").lstrip("./")


def _ts_project_node(project_name, repo_uri, proj_id, target_sha, ref) -> GraphNode:
    return GraphNode(
        "Project", {"id": proj_id},
        {"name": project_name, "repo_uri": repo_uri, "commit_sha": target_sha, "ref": ref},
    )


def _ts_referenced_names(rf) -> set[str]:
    """A generous superset of the simple names a file references cross-file: call
    names/receivers (+ `constructor` for `new X()`), heritage names, and every type ref.
    Over-collecting is safe; the equivalence oracle catches any under-collection."""
    names: set[str] = set()

    def callable_(c) -> None:
        for call in c.calls:
            names.add(call.name)
            if call.receiver:
                names.add(call.receiver)
            if getattr(call, "is_new", False):
                names.add("constructor")
        names.update(c.return_type_refs)
        for p in c.params:
            names.update(p.type_refs)

    for fn in rf.functions:
        callable_(fn)
    for cls in rf.classes:
        names.update(cls.extends)
        names.update(getattr(cls, "implements", []) or [])
        for p in cls.properties:
            names.update(p.type_refs)
        for m in cls.methods:
            callable_(m)
    for it in rf.interfaces:
        names.update(it.extends)
        for p in it.properties:
            names.update(p.type_refs)
        for m in it.methods:
            callable_(m)
    for ta in rf.type_aliases:
        names.update(ta.type_refs)
    return names


def read_graph_index(writer: GraphWriter, proj_id: str, exclude_keys: set[str], names: set[str]) -> resolution.GraphIndex:
    """Reconstruct TS resolution's `_Scope` symbol table from the graph for every module EXCEPT
    `exclude_keys` (the emit set). v2: the type/function/method maps are read only for symbols
    whose simple name is in `names` (what the blast radius references); the small module list
    is read in full (needed for import resolution). A node belongs to an excluded module iff
    its id is under that module's id prefix -- robust to dotted filenames."""
    exclude_prefixes = [ids.module_id(proj_id, k) for k in exclude_keys]

    def excluded(node_id: str) -> bool:
        return any(node_id == p or node_id.startswith(p + ".") for p in exclude_prefixes)

    lo, hi = _range(proj_id)
    index = resolution.GraphIndex()
    name_list = list(names)

    for path, mid in writer.ro_query(
        "MATCH (m:Module) WHERE m.id >= $lo AND m.id < $hi RETURN m.path, m.id", {"lo": lo, "hi": hi}
    ).result_set:
        if not excluded(mid):
            index.module_by_key[posixpath.normpath(path)] = mid

    if not name_list:
        return index

    for label in ("Class", "Interface", "Enum", "TypeAlias"):
        for name, nid in writer.ro_query(
            f"MATCH (t:{label}) WHERE t.id >= $lo AND t.id < $hi AND t.name IN $names RETURN t.name, t.id",
            {"lo": lo, "hi": hi, "names": name_list},
        ).result_set:
            if not excluded(nid):
                index.types.append((name, label, nid))
                if label == "Class":
                    index.classes.append((name, nid))

    for name, nid in writer.ro_query(
        "MATCH (f:Function) WHERE f.id >= $lo AND f.id < $hi AND f.name IN $names RETURN f.name, f.id",
        {"lo": lo, "hi": hi, "names": name_list},
    ).result_set:
        if not excluded(nid):
            index.functions.append((name, nid))

    # methods_of_class / method_index cover CLASS methods only (interface methods aren't call
    # targets) -- exactly what _build_scope collects -- so read them via Class-[:HAS_METHOD].
    for cid, mname, mid in writer.ro_query(
        "MATCH (c:Class)-[:HAS_METHOD]->(m:Method) WHERE c.id >= $lo AND c.id < $hi AND m.name IN $names "
        "RETURN c.id, m.name, m.id",
        {"lo": lo, "hi": hi, "names": name_list},
    ).result_set:
        if not excluded(cid):
            index.methods.append((cid, mname, mid))
    return index


def _ts_is_broken(rf) -> bool:
    """Whether a parse is too broken to emit (keep the file's last-good subgraph instead).

    tree-sitter is error-tolerant: not only does a mid-edit syntactically-broken file still yield a
    partial tree, but so does perfectly VALID modern TypeScript that the tree-sitter-typescript
    grammar can't fully model -- which sets ``has_error`` on good code. Treating ANY ``has_error`` as
    broken (the old ``lambda rf: rf.has_error``) meant such a file kept its STALE last-good subgraph
    on every incremental resync and never updated again (found on zod's 5k-line ``schemas.ts``: a new
    top-level function was silently never added). So refuse only a parse that recovered NOTHING usable
    -- otherwise emit the recovered symbols best-effort, exactly as a full ingest does, keeping
    incremental == full. This aligns TS with the other tree-sitter languages (C/Go/Rust/Java all emit
    ``has_error`` files). A total parse failure (no symbols recovered) still preserves last-good."""
    if not rf.has_error:
        return False
    return not (rf.functions or rf.classes or rf.interfaces or rf.type_aliases or rf.enums)


TS_SPEC = ScopedSpec(
    language="typescript",
    node_schemas=tuple(NODE_SCHEMAS),
    node_labels=tuple(s.label for s in NODE_SCHEMAS),
    file_suffixes=tuple(_FILE_SUFFIXES),
    excluded_dir_names=frozenset(_EXCLUDED_DIR_NAMES),
    symbol_ref_rels=_SYMBOL_REF_RELS,
    # CALLS_API (ADR 0006) is source-owned like a call: a file that drops a fetch() must diff its edge away.
    lateral_rels=_SYMBOL_REF_RELS + ("IMPORTS", "CALLS_API"),
    containment_rels=_CONTAINMENT_RELS,
    targetable_labels=("Class", "Interface", "Enum", "TypeAlias", "Function", "Method"),
    import_rel="IMPORTS",
    import_target_labels=("Module",),
    project_id=ids.project_id,
    scope_id_for_path=ids.module_id,
    scope_key_for_path=_scope_key,
    parse=parse_source,
    is_broken=_ts_is_broken,  # emit a `has_error` parse that recovered symbols (see _ts_is_broken)
    referenced_names=_ts_referenced_names,
    read_index=read_graph_index,
    resolve_incremental=resolution.resolve_incremental,
    project_node=_ts_project_node,
)


def incremental_ts_sync(writer, source, target_sha, changes, *, project_name, repo_uri, ref=None):
    """Thin wrapper over the shared engine used by the equivalence oracle (the git_sync routing
    goes through core.route_incremental → the same engine)."""
    return incremental_sync(writer, TS_SPEC, source, target_sha, changes,
                            project_name=project_name, repo_uri=repo_uri, ref=ref)
