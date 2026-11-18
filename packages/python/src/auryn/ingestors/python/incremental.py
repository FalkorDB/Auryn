"""Python (`ast` engine) binding for the generic incremental resync engine
(core/incremental_scoped.py, ADR 0002).

Everything mechanical -- the git changed-set → graph affected-closure → scoped emit/read/diff
-- lives in the shared engine. This module supplies only the Python-specific pieces via
`PYTHON_SPEC`: the id↔scope mapping, the edge classification, and the two genuinely
language-specific functions -- reconstructing the resolution symbol table from the graph
(`read_graph_index`) and emitting a subset against it (`resolution.resolve_incremental`).

Correctness (== a full recompute) is enforced by tests/ingestors/
test_python_incremental_equivalence.py. Fast (`ast`) engine only; the deep (jedi) engine's
transitive type ripples are out of scope. A *newly-satisfiable* import IS covered by the
module-level IMPORTS breadcrumb; the residual gaps (importing a not-yet-existing module,
cross-module MRO ordering) are documented on resolve_incremental.
"""

from __future__ import annotations

import re
from collections import defaultdict

from auryn.core.graph_client import GraphWriter
from auryn.core.graph_model import GraphNode
from auryn.core.incremental_scoped import ChangeSet, ScopedSpec, incremental_sync
from auryn.ingestors.python import ids, resolution
from auryn.ingestors.python.file_parser import parse_module
from auryn.ingestors.python.git_sync import GitPythonProjectConfig, SyncStats
from auryn.ingestors.python.plugin import EXCLUDED_DIR_NAMES as _EXCLUDED_DIR_NAMES
from auryn.ingestors.python.schema import NODE_SCHEMAS

__all__ = ["ChangeSet", "incremental_ast_sync", "PYTHON_SPEC", "read_graph_index"]

_CONTAINMENT_RELS = (
    "HAS_PACKAGE", "HAS_SUBPACKAGE", "HAS_MODULE", "HAS_CLASS", "HAS_FUNCTION",
    "HAS_METHOD", "HAS_NESTED_CLASS", "HAS_ATTRIBUTE", "HAS_PARAMETER",
)
_SYMBOL_REF_RELS = ("CALLS", "EXTENDS", "IMPLEMENTS", "RETURNS")


def _range(prefix: str) -> tuple[str, str]:
    return prefix, prefix + chr(0xFFFF)


def _scope_id(proj_id: str, path: str, package_dirs: frozenset[str] | None = None) -> str:
    """The id prefix owning all of a file's entities: package_id for an __init__.py, else
    module_id. `package_dirs` is the ingest-wide import-root context (see incremental_scoped's
    build_path_context); it must match what `parse`/`scope_key_for_path` use so the scope id lines
    up with the module id resolution emits."""
    dotted = ids.dotted_module_path(path, package_dirs)
    return ids.package_id(proj_id, dotted) if ids.is_package_init(path) else ids.module_id(proj_id, dotted)


def _module_dotted_from_id(node_id: str) -> str | None:
    for marker in ("/module/", "/package/"):
        if marker in node_id:
            rest = node_id.split(marker, 1)[1]
            for sep in (".class.", ".func.", ".method.", ".attr.", ".param."):
                if sep in rest:
                    return rest.split(sep, 1)[0]
            return rest
    return None


def _python_project_node(project_name, repo_uri, proj_id, target_sha, ref) -> GraphNode:
    return GraphNode(
        "Project",
        {"id": proj_id},
        {"name": project_name, "repo_uri": repo_uri, "commit_sha": target_sha, "ref": ref, "depth": "fast"},
    )


_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _python_referenced_names(m) -> set[str]:
    """A generous superset of the simple names a module references cross-file: call
    names/receivers, tokens of every type annotation and base expression, and import names.
    Over-collecting is safe (extra graph lookups); under-collecting would drop an edge, which
    the equivalence oracle catches. See read_graph_index (v2 targeting)."""
    names: set[str] = set()

    def add_expr(expr: str | None) -> None:
        if expr:
            names.update(_IDENT.findall(expr))

    def fn(f) -> None:
        for c in f.calls:
            names.add(c.name)
            if c.receiver:
                names.add(c.receiver)
        add_expr(f.returns)
        for p in f.params:
            add_expr(p.annotation)
        for n in f.nested:  # nested defs are their own nodes; collect the names their bodies reference
            fn(n)

    def cls(c) -> None:
        for b in c.bases:
            add_expr(b)
        for a in c.class_attrs:
            add_expr(a.annotation)
        for meth in c.methods:
            fn(meth)
            for a in meth.self_attrs:
                add_expr(a.annotation)
        for nested in c.nested:
            cls(nested)

    for f in m.functions:
        fn(f)
    for c in m.classes:
        cls(c)
    for call in m.calls:  # module-scope (top-level) calls: same cross-file names a function body has
        names.add(call.name)
        if call.receiver:
            names.add(call.receiver)
    for imp in m.imports:
        names.add(imp.local)
        names.update(imp.target.split("."))
    return names


def read_graph_index(writer: GraphWriter, proj_id: str, exclude_keys: set[str], names: set[str]) -> resolution.GraphIndex:
    """Reconstruct resolution's cross-file symbol table from the graph for every module EXCEPT
    `exclude_keys` (the emit set). v2: the big class/callable maps are read only for symbols
    whose simple name is in `names` (what the blast radius references) -- an indexed lookup
    instead of the whole project. The small package/module lists and inheritance `parents`
    (needed transitively for MRO) are still read in full."""
    lo, hi = _range(proj_id)
    index = resolution.GraphIndex()
    name_list = list(names)

    for (dotted,) in writer.ro_query(
        "MATCH (p:Package) WHERE p.id >= $lo AND p.id < $hi RETURN p.dotted_path", {"lo": lo, "hi": hi}
    ).result_set:
        if dotted not in exclude_keys:
            index.packages.add(dotted)
            index.module_dotteds.add(dotted)
    for (dotted,) in writer.ro_query(
        "MATCH (m:Module) WHERE m.id >= $lo AND m.id < $hi RETURN m.dotted_path", {"lo": lo, "hi": hi}
    ).result_set:
        if dotted not in exclude_keys:
            index.module_dotteds.add(dotted)

    if name_list:
        for qname, cid in writer.ro_query(
            "MATCH (c:Class) WHERE c.id >= $lo AND c.id < $hi AND c.name IN $names RETURN c.qualified_name, c.id",
            {"lo": lo, "hi": hi, "names": name_list},
        ).result_set:
            if _module_dotted_from_id(cid) not in exclude_keys:
                index.class_by_qname[qname] = cid
        for label in ("Function", "Method"):
            for qname, nid in writer.ro_query(
                f"MATCH (f:{label}) WHERE f.id >= $lo AND f.id < $hi AND f.name IN $names "
                "RETURN f.qualified_name, f.id",
                {"lo": lo, "hi": hi, "names": name_list},
            ).result_set:
                if _module_dotted_from_id(nid) not in exclude_keys:
                    index.callable_by_qname[qname] = (nid, label)

    parents: dict[str, list[str]] = defaultdict(list)
    for cid, cq, bq in writer.ro_query(
        "MATCH (c:Class)-[:EXTENDS|IMPLEMENTS]->(b:Class) WHERE c.id >= $lo AND c.id < $hi "
        "RETURN c.id, c.qualified_name, b.qualified_name",
        {"lo": lo, "hi": hi},
    ).result_set:
        if _module_dotted_from_id(cid) not in exclude_keys:
            parents[cq].append(bq)
    index.parents = dict(parents)
    return index


PYTHON_SPEC = ScopedSpec(
    language="python",
    node_schemas=tuple(NODE_SCHEMAS),
    node_labels=tuple(s.label for s in NODE_SCHEMAS),
    file_suffixes=(".py",),
    excluded_dir_names=frozenset(_EXCLUDED_DIR_NAMES),
    symbol_ref_rels=_SYMBOL_REF_RELS,
    lateral_rels=_SYMBOL_REF_RELS + ("IMPORTS",),
    containment_rels=_CONTAINMENT_RELS,
    targetable_labels=("Function", "Method", "Class"),
    import_rel="IMPORTS",
    import_target_labels=("Module", "Package"),
    project_id=ids.project_id,
    scope_id_for_path=_scope_id,
    scope_key_for_path=ids.dotted_module_path,
    parse=parse_module,
    build_path_context=ids.package_dirs_from_paths,
    is_broken=lambda m: not m.parse_ok,  # ast raised SyntaxError -> keep last-good
    referenced_names=_python_referenced_names,
    read_index=read_graph_index,
    resolve_incremental=resolution.resolve_incremental,
    project_node=_python_project_node,
)


def incremental_ast_sync(writer, config: GitPythonProjectConfig, source, target_sha, changes, ref=None) -> SyncStats:
    """Thin wrapper over the shared engine that returns Python's SyncStats (the oracle and the
    git_sync routing both go through here / route_incremental → the same engine)."""
    s = incremental_sync(writer, PYTHON_SPEC, source, target_sha, changes,
                         project_name=config.project_name, repo_uri=config.repo_uri, ref=ref)
    return SyncStats(
        nodes_added=s.nodes_added, nodes_updated=s.nodes_updated, nodes_removed=s.nodes_removed,
        edges_added=s.edges_added, edges_updated=s.edges_updated, edges_removed=s.edges_removed,
        commit_sha=s.commit_sha, elapsed_seconds=s.elapsed_seconds,
        files_parsed=s.files_parsed, files_cached=s.files_cached,
    )
