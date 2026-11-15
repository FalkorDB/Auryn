"""Git-tracked (and directory) Go ingestion: moves a project's Go subgraph to a target
commit/tree by fully recomputing the target state and diffing it against the graph
(graph_sync.compute_delta) -- the same recompute-and-delta model as the Java/C/C#/Python
ingestors (resolution is global -- the package-wide type/func/method index -- so a file-level
diff can't tell which untouched files a change affects).

Fast engine only for now: tree-sitter-go streams each `.go` blob (no disk materialization, no
build config) -> RawFile -> resolution.resolve(). A deep go/types or gopls pass (structural
IMPLEMENTS, interface-dispatch/cross-file calls, inferred local-variable types) would be added
as the GitLanguage's `deep_sync` (see plugin.py). Registered as the `go` GitLanguage for the
multi-language endpoint (core/ingest.py) in both ingest_repository (git) and ingest_directory
(local)."""

from __future__ import annotations

import itertools
import logging
import time
from dataclasses import dataclass

from auryn.core.graph_model import GraphNode
from auryn.core.graph_sync import stream_apply
from auryn.core.parallel import parse_blobs_ordered
from auryn.core.registry import LanguageIngestResult
from auryn.ingestors.go import gomod, resolution
from auryn.ingestors.go.file_parser import parse_source
from auryn.ingestors.go.ids import module_id, project_id
from auryn.ingestors.go.plugin import EXCLUDED_DIR_NAMES as _EXCLUDED_DIR_NAMES
from auryn.ingestors.go.plugin import FILE_SUFFIXES as _FILE_SUFFIXES
from auryn.ingestors.go.schema import NODE_SCHEMAS

logger = logging.getLogger(__name__)


@dataclass
class SyncStats:
    nodes_added: int = 0
    nodes_updated: int = 0
    nodes_removed: int = 0
    edges_added: int = 0
    edges_updated: int = 0
    edges_removed: int = 0
    commit_sha: str = ""
    elapsed_seconds: float = 0.0
    files_parsed: int = 0
    files_cached: int = 0


def sync_at(writer, source, target_sha: str, project_name: str, repo_uri: str, ref: str | None) -> SyncStats:
    """Recompute the whole Go subgraph at `target_sha` and delta it onto the graph. `source` is
    duck-typed (GitRepoCache or LocalDirectorySource): only list_files/read_blob used."""
    started = time.monotonic()
    proj_id = project_id(project_name)

    writer.setup_schema(NODE_SCHEMAS)  # brings the graph key into existence before the first read
    current_sha = writer.get_property("Project", {"id": proj_id}, "commit_sha")
    if current_sha == target_sha:
        return SyncStats(commit_sha=target_sha, elapsed_seconds=time.monotonic() - started)

    # go.mod module map (module_dir -> module_path), from the manifests at the target. It turns each
    # package dir into its canonical import path (the cross-repo linkage key); the node ids stay
    # dir-keyed (pure path), so a package's identity doesn't depend on it.
    modules = _read_modules(source, target_sha)
    module_map = gomod.build_module_map(modules)

    # Incremental resync (ADR 0005, authored-shared engine): when a prior graph exists at a
    # different, reachable commit, move it by processing only the git diff + its graph-derived blast
    # radius. GUARD: a go.mod STRUCTURAL change (a module path added/removed/renamed) shifts the
    # import paths -- and thus the linkage keys -- of files that did NOT change, which an incremental
    # pass would not re-emit; fall back to a full recompute so incremental == full always holds.
    if current_sha and current_sha != target_sha and _module_map_from_graph(writer, proj_id) == module_map:
        incr = _route(writer, source, target_sha, ref, current_sha,
                      project_name=project_name, repo_uri=repo_uri)
        if incr is not None:
            _reconcile_gomod(writer, proj_id, modules)
            return _finish(writer, proj_id, source, incr)

    entries = [
        (path, blob_sha)
        for suffix in _FILE_SUFFIXES
        for path, blob_sha in source.list_files(target_sha, suffix, set(_EXCLUDED_DIR_NAMES))
    ]
    files = parse_blobs_ordered(
        entries, source.read_blob, parse_source,
        preload=["auryn.ingestors.go.file_parser"], logger=logger,
    )

    project_node = GraphNode(
        "Project", {"id": proj_id}, {"name": project_name, "repo_uri": repo_uri, "commit_sha": target_sha, "ref": ref}
    )

    def make_target():  # re-iterable: stream_apply runs it once for nodes, once for edges
        return itertools.chain([project_node], resolution.resolve(files, proj_id, module_map))

    result = _apply(writer, proj_id, make_target, target_sha, started, files_parsed=len(files))
    _reconcile_gomod(writer, proj_id, modules)
    return _finish(writer, proj_id, source, result)


def _route(writer, source, target_sha, ref, current_sha, *, project_name, repo_uri) -> "SyncStats | None":
    """Attempt an incremental resync via the shared engine's git routing; return SyncStats or None
    to fall back to the full recompute. GO_SPEC sets `provenance_rels`, so route_incremental
    dispatches to the authored-shared engine (incremental_sync_authored)."""
    from auryn.core.incremental_scoped import route_incremental
    from auryn.ingestors.go.incremental import GO_SPEC

    s = route_incremental(
        writer, GO_SPEC, source, target_sha, ref, current_sha,
        project_name=project_name, repo_uri=repo_uri, logger=logger,
    )
    if s is None:
        return None
    return SyncStats(
        nodes_added=s.nodes_added, nodes_updated=s.nodes_updated, nodes_removed=s.nodes_removed,
        edges_added=s.edges_added, edges_updated=s.edges_updated, edges_removed=s.edges_removed,
        commit_sha=s.commit_sha, elapsed_seconds=s.elapsed_seconds,
        files_parsed=s.files_parsed, files_cached=s.files_cached,
    )


def _finish(writer, proj_id: str, source, result: SyncStats) -> SyncStats:
    """Record the working-tree base for the next save-triggered incremental resync (no-op for
    git/plain-dir sources)."""
    from auryn.core.incremental_scoped import stamp_worktree
    from auryn.core.local_source import WorkingTreeSource

    if isinstance(source, WorkingTreeSource):
        stamp_worktree(writer, proj_id, source, _FILE_SUFFIXES)
    return result


def _apply(writer, proj_id: str, make_target, target_sha: str, started: float, files_parsed: int) -> SyncStats:
    # Cross-repo reconciliation (ADR 0004): fold placeholder<->definer pairs among the linkages
    # this ingest touched -- targeted index seeks, no scan. No-op unless the graph holds >1 repo.
    from auryn.core.linkage import reconcile_linkages

    # stream_apply writes the delta without materialising the target graph, and returns the
    # linkage strings collected inline (see graph_sync.stream_apply / core.linkage).
    labels = [s.label for s in NODE_SCHEMAS]
    stats, reals, placeholders = stream_apply(writer, proj_id, labels, make_target)
    reconcile_linkages(writer, reals, placeholders)

    return SyncStats(
        nodes_added=stats.nodes_added,
        nodes_updated=stats.nodes_updated,
        nodes_removed=stats.nodes_removed,
        edges_added=stats.edges_added,
        edges_updated=stats.edges_updated,
        edges_removed=stats.edges_removed,
        commit_sha=target_sha,
        elapsed_seconds=time.monotonic() - started,
        files_parsed=files_parsed,
        files_cached=0,
    )


_GO_MOD = "go.mod"


def _read_modules(source, target_sha: str) -> list:
    """Parse every `go.mod` at the target into a list of `gomod.GoModule`."""
    mods = []
    for rel, blob in source.list_files(target_sha, _GO_MOD, set(_EXCLUDED_DIR_NAMES)):
        mdir = rel[: -len(_GO_MOD)].rstrip("/")
        mods.append(gomod.parse_gomod(mdir, source.read_blob(blob)))
    return mods


def _module_map_from_graph(writer, proj_id: str) -> dict:
    """Reconstruct `{module_dir: module_path}` from the graph's Module nodes -- compared against the
    target manifests to decide whether a go.mod structural change forces a full recompute."""
    lo, hi = proj_id + "/", proj_id + "/" + chr(0xFFFF)
    return {
        str(mdir): str(mpath) for mdir, mpath in writer.ro_query(
            "MATCH (m:Module) WHERE m.id >= $lo AND m.id < $hi RETURN m.dir, m.name", {"lo": lo, "hi": hi}
        ).result_set
    }


def _reconcile_gomod(writer, proj_id: str, modules: list) -> None:
    """Layer the manifest-only facts onto the Module tier (go version + crate-to-crate require
    DEPENDS_ON), deterministically from the go.mod set -- so the differential oracle's
    incremental == full holds (the delta callbacks can't read a go.mod; this step, which has source,
    is where that data enters). Runs in BOTH the full and incremental paths.

    A Module is MERGED (not just enriched): resolution authors it from the module map and the
    aggregation refcount GCs it when its last .go file goes, but a manifest-backed module must
    OUTLIVE its source files (a full ingest emits it from the go.mod regardless) -- so we recreate it
    here, stamping the shared `:Entity` label the normal upsert path adds, and (re)attach it to the
    Project. `_module_map_from_graph` therefore always mirrors the go.mod set, keeping the structural-
    change fallback exact."""
    path_to_dir = {m.module_path: m.dir for m in modules if m.module_path}
    desired: set[tuple[str, str]] = set()
    for m in modules:
        if m.module_path is None:
            continue
        external = []
        for req in m.requires:
            if req.indirect:
                continue
            if req.module_path in path_to_dir and path_to_dir[req.module_path] != m.dir:
                desired.add((module_id(proj_id, m.dir), module_id(proj_id, path_to_dir[req.module_path])))
            elif req.module_path not in path_to_dir:
                external.append(f"{req.module_path} {req.version}".strip())
        writer.query(
            "MERGE (mod:Module {id: $id}) "
            "SET mod:Entity, mod.name = $name, mod.qualified_name = $name, mod.dir = $dir, "
            "mod.go_version = $gv, mod.external_requires = $ext "
            "WITH mod MATCH (p:Project {id: $proj}) MERGE (p)-[:HAS_MODULE]->(mod)",
            {"id": module_id(proj_id, m.dir), "name": m.module_path, "dir": m.dir,
             "proj": proj_id, "gv": m.go_version, "ext": sorted(set(external))},
        )

    existing = writer.ro_query(
        "MATCH (a:Module)-[:DEPENDS_ON]->(b:Module) WHERE a.id STARTS WITH $pre RETURN a.id, b.id",
        {"pre": proj_id + "/module"},
    ).result_set
    for a, b in existing:
        if (str(a), str(b)) not in desired:
            writer.query("MATCH (a:Module {id: $a})-[r:DEPENDS_ON]->(b:Module {id: $b}) DELETE r",
                         {"a": str(a), "b": str(b)})
    for a, b in sorted(desired):
        writer.query(
            "MATCH (a:Module {id: $a}), (b:Module {id: $b}) "
            "MERGE (a)-[r:DEPENDS_ON]->(b) SET r.kind = 'gomod', r.resolution = 'name'",
            {"a": a, "b": b},
        )


def _git_language_sync(
    writer,
    source,
    target_sha: str,
    *,
    repo_uri: str,
    project_name: str,
    solution_name: str | None,
    ref: str | None,
    cache_root=None,
) -> LanguageIngestResult:
    """GitLanguage adapter for the multi-language endpoint (Go has no solution/cache knobs --
    accepted for the shared interface and ignored)."""
    s = sync_at(writer, source, target_sha, project_name=project_name, repo_uri=repo_uri, ref=ref)
    return LanguageIngestResult(
        nodes_added=s.nodes_added,
        nodes_updated=s.nodes_updated,
        nodes_removed=s.nodes_removed,
        edges_added=s.edges_added,
        edges_updated=s.edges_updated,
        edges_removed=s.edges_removed,
        files_parsed=s.files_parsed,
        files_cached=s.files_cached,
        commit_sha=s.commit_sha,
        elapsed_seconds=s.elapsed_seconds,
    )


# The GitLanguage is registered by the import-light plugin.py (the entry-point target), whose
# lazy `sync` thunk calls `_git_language_sync` above -- so importing this tree-sitter engine
# module is deferred until a source actually contains Go files.
