"""Project-wide pass (phase 2): turn the per-file RawFile list into graph nodes/edges, applying
the cross-file decisions a single file can't -- resolving each function-body call to a project
function or an external command, and each `source`/`.` include to a project file or an external
file.

Name-based only -- this is the fast tree-sitter engine, so every resolved CALLS/IMPORTS edge is
stamped `resolution="name"`. Bash functions are effectively global once their file is `source`d
(there are no packages, modules or visibility modifiers), so a call resolves to a project function
by bare name across the whole project. Unresolved-but-nameable references become upgradable
placeholder nodes reached by ordinary edges (ADR 0004), stamped `resolution="external"`:

- an invoked command that is not a project function -> an external `Function` placeholder keyed
  `extern://bash/<name>` (a PATH binary like `git`/`ls`, or a function defined in a not-yet-ingested
  sibling repo's `source`d library). Shell builtins (`echo`, `printf`, ...) are dropped upstream.
- a `source`d path that resolves to no project file -> an external `File` placeholder keyed
  `extern://bash/file/<path>` (a system file like `/etc/profile`, or an unresolvable path).

There is deliberately NO type/class model, NO Parameter node, and NO IMPLEMENTS/EXTENDS edge:
Bash has none of those concepts, so the fast pass invents nothing the language doesn't have."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field

from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.ingestors.bash import ids
from auryn.ingestors.bash.raw_model import RawFile


@dataclass
class GraphIndex:
    """The symbol table reconstructable from the graph for every file NOT being re-emitted -- the
    project's other functions keyed by bare name (CALLS resolution) and every other file's
    (normalized path, id) (source/IMPORTS resolution). Seeds `resolve_incremental` so an
    incremental resync can emit only the changed+affected files while still resolving their
    cross-file references identically to a full recompute. See core/incremental_scoped.py and
    ADR 0002; correctness is enforced by tests/ingestors/bash/test_bash_incremental.py."""

    func_ids_by_name: dict[str, list[str]] = field(default_factory=dict)  # bare name -> [Function id]
    files_by_norm: list[tuple[str, str]] = field(default_factory=list)    # (normalized path, File id)


def resolve(raw_files: list[RawFile], project_id: str) -> Iterator[GraphNode | GraphEdge]:
    """Full-recompute resolution: every project file is present, so the symbol tables are built
    entirely from `raw_files` (no seed)."""
    yield from _resolve(raw_files, project_id, seed=None)


def resolve_incremental(
    emit_files: list[RawFile], project_id: str, seed: GraphIndex
) -> list[GraphNode | GraphEdge]:
    """Resolve+emit ONLY `emit_files` (the changed+affected set), binding their references against
    a symbol table = these files' own symbols merged over `seed` (every OTHER file's symbols, read
    back from the graph). Output is identical to `resolve`'s for these files -- verified by the
    equivalence oracle (tests/ingestors/bash/test_bash_incremental.py)."""
    return list(_resolve(emit_files, project_id, seed))


def _resolve(
    raw_files: list[RawFile], project_id: str, seed: GraphIndex | None
) -> Iterator[GraphNode | GraphEdge]:
    # -- files ------------------------------------------------------------------------------
    # `files_by_norm` is the SOURCE-resolution table (emit files + every seeded file); nodes/edges
    # are emitted for the emit files only. On a full recompute `seed` is None and this is just the
    # full project.
    file_ids: dict[str, str] = {}
    files_by_norm: list[tuple[str, str]] = list(seed.files_by_norm) if seed else []
    for rf in raw_files:
        fid = ids.file_id(project_id, rf.path)
        file_ids[rf.path] = fid
        norm = _norm(rf.path)
        files_by_norm.append((norm, fid))
        yield GraphNode("File", {"id": fid}, {"name": _basename(rf.path), "path": norm})
        yield GraphEdge("HAS_FILE", "Project", {"id": project_id}, "File", {"id": fid})

    # -- index functions (global by bare name across the project) ---------------------------
    # func_ids_by_name is the CALLS-resolution table: the emit files' functions merged over the
    # seed's (other files' functions). sorted()[0] at each call site therefore picks the same
    # project-wide winner the full recompute would.
    func_id_by: dict[tuple[str, str], str] = {}  # (file path, name) -> function id (emit files only)
    func_ids_by_name: dict[str, list[str]] = defaultdict(list)  # name -> [function id]
    if seed:
        for name, fids in seed.func_ids_by_name.items():
            func_ids_by_name[name].extend(fids)
    for rf in raw_files:
        for fn in rf.functions:
            fid = ids.function_id(file_ids[rf.path], fn.name)
            func_id_by[(rf.path, fn.name)] = fid
            func_ids_by_name[fn.name].append(fid)

    # -- pass 1: function nodes + containment (landing nodes for pass 2) --------------------
    for rf in raw_files:
        for fn in rf.functions:
            fid = func_id_by[(rf.path, fn.name)]
            yield GraphNode("Function", {"id": fid}, {
                "name": fn.name,
                "kind": "FUNCTION",
                "source": fn.source,
                "doc": fn.doc,
                "line_start": fn.line_start,
                "line_end": fn.line_end,
                # ADR 0004: a bash function is reachable by bare name once sourced -> its cross-repo
                # key is the bare name; a caller in another repo names it exactly the same way.
                "linkage_key": ids.extern_function_key(fn.name),
            })
            yield GraphEdge("HAS_FUNCTION", "File", {"id": file_ids[rf.path]}, "Function", {"id": fid})

    # -- pass 2: CALLS + IMPORTS ------------------------------------------------------------
    emitted_externs: set[str] = set()  # placeholder ids emitted this pass (dedup, ADR 0004)

    for rf in raw_files:
        for fn in rf.functions:
            from_id = func_id_by[(rf.path, fn.name)]
            emitted: set[str] = set()
            for call in fn.calls:
                targets = sorted(func_ids_by_name.get(call.name, []))
                if targets:  # resolved to a project function (bare-name, project-wide)
                    target_id = targets[0]
                    if target_id == from_id or target_id in emitted:
                        continue
                    emitted.add(target_id)
                    yield GraphEdge("CALLS", "Function", {"id": from_id}, "Function", {"id": target_id},
                                    {"resolution": "name"})
                    continue
                # external command -> upgradable placeholder Function (ADR 0004).
                eid = ids.extern_function_key(call.name)
                if eid in emitted:
                    continue
                emitted.add(eid)
                if eid not in emitted_externs:
                    emitted_externs.add(eid)
                    yield GraphNode("Function", {"id": eid}, {
                        "name": call.name, "qualified_name": call.name, "resolved": False,
                        "origin": "external", "linkage_key": eid,
                    })
                yield GraphEdge("CALLS", "Function", {"id": from_id}, "Function", {"id": eid},
                                {"resolution": "external"})

    # -- source / . includes -> IMPORTS -----------------------------------------------------
    for rf in raw_files:
        from_file = file_ids[rf.path]
        emitted_imports: set[str] = set()
        for src in rf.sources:
            if src.is_dynamic:
                continue  # a `$DIR/lib.sh` path can't be resolved statically -> skip
            target = _resolve_source(src.path, files_by_norm)
            if target is not None:  # resolves to a project file
                if target == from_file or target in emitted_imports:
                    continue
                emitted_imports.add(target)
                yield GraphEdge("IMPORTS", "File", {"id": from_file}, "File", {"id": target},
                                {"resolution": "name"})
                continue
            # external / unresolvable file -> placeholder File (ADR 0004 shape; not a linkable
            # callable, so it carries no linkage_key and is skipped by reconciliation).
            eid = ids.extern_file_key(src.path)
            if eid in emitted_imports:
                continue
            emitted_imports.add(eid)
            if eid not in emitted_externs:
                emitted_externs.add(eid)
                yield GraphNode("File", {"id": eid}, {
                    "name": _basename(src.path), "path": src.path, "resolved": False,
                    "origin": "external",
                })
            yield GraphEdge("IMPORTS", "File", {"id": from_file}, "File", {"id": eid},
                            {"resolution": "external"})


# -- source path resolution ----------------------------------------------------------------

def _resolve_source(raw_path: str, files_by_norm: list[tuple[str, str]]) -> str | None:
    """Best-effort match of a `source`d path to a project file id, or None. Bash source paths are
    relative to the caller's cwd/script dir (ambiguous statically), so we match by exact normalized
    path, then by path suffix, then by basename -- taking the deterministic first candidate."""
    target = _norm(raw_path)
    if not target:
        return None
    exact = sorted(fid for (norm, fid) in files_by_norm if norm == target)
    if exact:
        return exact[0]
    suffix = sorted(fid for (norm, fid) in files_by_norm if norm.endswith("/" + target))
    if suffix:
        return suffix[0]
    base = target.rsplit("/", 1)[-1]
    byname = sorted(fid for (norm, fid) in files_by_norm if norm.rsplit("/", 1)[-1] == base)
    if len(byname) == 1:  # basename match only when unambiguous
        return byname[0]
    return None


# -- helpers -------------------------------------------------------------------------------

def _norm(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def _basename(path: str) -> str:
    return _norm(path).rsplit("/", 1)[-1]
