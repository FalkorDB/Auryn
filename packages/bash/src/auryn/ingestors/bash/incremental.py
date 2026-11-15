"""Bash binding for the generic incremental resync engine (core/incremental_scoped.py, ADR 0002).

Bash is the simplest file-scoped language on the roster: a script IS a file, every `Function`
nests under its defining `File`'s id (`<file_id>.func.<name>`), and the only cross-file edges are
`CALLS` (Function→Function, resolved by bare name project-wide) and `IMPORTS` (File→File, from a
`source`/`.` include). So every entity of a file lives under a single per-file id prefix -- exactly
the invariant the engine requires -- and this module supplies only the Bash-specific glue via
`BASH_SPEC`: id↔scope mapping, edge classification, and the two genuinely language-specific pieces
-- reconstructing the resolution symbol table from the graph (`read_graph_index`) and emitting a
subset against it (`resolution.resolve_incremental`).

Correctness (an incremental resync == a full recompute, node-for-node/edge-for-edge, including the
reclamation of now-orphaned `extern://` command placeholders) is enforced by
tests/ingestors/bash/test_bash_incremental.py.
"""

from __future__ import annotations

from auryn.core.graph_client import GraphWriter
from auryn.core.graph_model import GraphNode
from auryn.core.incremental_scoped import ChangeSet, ScopedSpec, incremental_sync
from auryn.ingestors.bash import ids, resolution
from auryn.ingestors.bash.file_parser import parse_source
from auryn.ingestors.bash.plugin import EXCLUDED_DIR_NAMES as _EXCLUDED_DIR_NAMES
from auryn.ingestors.bash.plugin import FILE_SUFFIXES as _FILE_SUFFIXES
from auryn.ingestors.bash.schema import NODE_SCHEMAS

__all__ = ["ChangeSet", "BASH_SPEC", "read_graph_index", "incremental_bash_sync"]

# Edge classification for the engine (see core/incremental_scoped.read_scoped_fingerprints):
#  - containment (target-owned): the structural spine. HAS_FILE's target File and HAS_FUNCTION's
#    target Function both nest under the emit scope, so a file re-emits exactly the containment
#    edges whose target it owns.
#  - symbol-ref (source-owned): CALLS points at a Function symbol -- walked for the reverse-dep
#    closure (a caller of a changed function must be re-resolved).
#  - lateral (source-owned): CALLS + IMPORTS, both sourced at a node in the emit scope.
_CONTAINMENT_RELS = ("HAS_FILE", "HAS_FUNCTION")
_SYMBOL_REF_RELS = ("CALLS",)


def _range(prefix: str) -> tuple[str, str]:
    return prefix, prefix + chr(0xFFFF)


def _scope_key(path: str) -> str:
    """A file's stable scope key = its normalized path -- the exact string `File.path` stores and
    `file_id` is built from, so it agrees with the id-prefix the engine scopes by."""
    return path.replace("\\", "/").lstrip("./")


def _bash_project_node(project_name, repo_uri, proj_id, target_sha, ref) -> GraphNode:
    return GraphNode(
        "Project", {"id": proj_id},
        {"name": project_name, "repo_uri": repo_uri, "commit_sha": target_sha, "ref": ref},
    )


def _bash_satisfied_linkage_keys(raws) -> set[str]:
    """The command-placeholder linkages the emit set's function definitions can satisfy: every
    function a just-parsed file DEFINES is reachable project-wide by bare name, so an untouched
    caller that currently points at `extern://bash/<name>` should be re-resolved onto it. Drives
    the engine's placeholder-satisfaction closure (bare-name resolution has no dependency edge for
    the standard closures to follow)."""
    return {ids.extern_function_key(fn.name) for rf in raws for fn in rf.functions}


def _bash_referenced_names(rf) -> set[str]:
    """The bare command names this file's functions invoke -- the only names it can resolve to a
    project function cross-file. (Source paths are matched against the full file list, not by name,
    so they need no name seed.) Over-collecting is safe; the equivalence oracle catches an
    under-collection."""
    return {call.name for fn in rf.functions for call in fn.calls}


def read_graph_index(
    writer: GraphWriter, proj_id: str, exclude_keys: set[str], names: set[str]
) -> resolution.GraphIndex:
    """Reconstruct Bash resolution's symbol tables from the graph for every file EXCEPT
    `exclude_keys` (the emit + removed set): the full file list (for `source`/IMPORTS resolution,
    which matches by path/suffix/basename) and the project functions whose bare name is in `names`
    (what the blast radius calls). A node belongs to an excluded file iff its id is that file's id
    or nests under it (`<file_id>.…`) -- robust to dotted filenames. `extern://` placeholders sort
    outside the project id range, so the range scan skips them for free."""
    exclude_prefixes = [ids.file_id(proj_id, k) for k in exclude_keys]

    def excluded(node_id: str) -> bool:
        return any(node_id == p or node_id.startswith(p + ".") for p in exclude_prefixes)

    lo, hi = _range(proj_id)
    index = resolution.GraphIndex()

    for path, fid in writer.ro_query(
        "MATCH (f:File) WHERE f.id >= $lo AND f.id < $hi RETURN f.path, f.id", {"lo": lo, "hi": hi}
    ).result_set:
        if not excluded(fid):
            index.files_by_norm.append((_scope_key(path), fid))

    name_list = list(names)
    if name_list:
        for name, fid in writer.ro_query(
            "MATCH (f:Function) WHERE f.id >= $lo AND f.id < $hi AND f.name IN $names RETURN f.name, f.id",
            {"lo": lo, "hi": hi, "names": name_list},
        ).result_set:
            if not excluded(fid):
                index.func_ids_by_name.setdefault(name, []).append(fid)
    return index


BASH_SPEC = ScopedSpec(
    language="bash",
    node_schemas=tuple(NODE_SCHEMAS),
    node_labels=tuple(s.label for s in NODE_SCHEMAS),
    file_suffixes=tuple(_FILE_SUFFIXES),
    excluded_dir_names=frozenset(_EXCLUDED_DIR_NAMES),
    symbol_ref_rels=_SYMBOL_REF_RELS,
    lateral_rels=_SYMBOL_REF_RELS + ("IMPORTS",),
    containment_rels=_CONTAINMENT_RELS,
    targetable_labels=("Function",),
    import_rel="IMPORTS",
    import_target_labels=("File",),
    project_id=ids.project_id,
    scope_id_for_path=ids.file_id,
    scope_key_for_path=_scope_key,
    parse=parse_source,
    # tree-sitter-bash recovers from errors and the RawFile carries no error flag, so a "broken"
    # file is just its best-effort parse -- identical in both engines, so accepting it keeps
    # incremental == full recompute.
    is_broken=lambda rf: False,
    referenced_names=_bash_referenced_names,
    read_index=read_graph_index,
    resolve_incremental=resolution.resolve_incremental,
    project_node=_bash_project_node,
    satisfied_linkage_keys=_bash_satisfied_linkage_keys,
)


def incremental_bash_sync(writer, source, target_sha, changes, *, project_name, repo_uri, ref=None):
    """Thin wrapper over the shared engine used by the equivalence oracle (the git_sync routing
    goes through core.route_incremental → the same engine)."""
    return incremental_sync(writer, BASH_SPEC, source, target_sha, changes,
                            project_name=project_name, repo_uri=repo_uri, ref=ref)
