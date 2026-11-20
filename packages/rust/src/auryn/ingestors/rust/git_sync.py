"""Git-tracked (and directory) Rust ingestion: moves a project's Rust subgraph to a target
commit/tree by fully recomputing the target state and diffing it against the graph
(graph_sync.compute_delta) -- the same recompute-and-delta model as the Java/C/C#/Python
ingestors (resolution is global -- the project-wide type/method/module index -- so a file-level
diff can't tell which untouched files a change affects).

Fast engine only for now: tree-sitter-rust streams each `.rs` blob (no disk materialization, no
cargo build) -> RawFile -> resolution.resolve(). A deep rust-analyzer / rustc HIR pass (resolved
types, `use`-path resolution, trait-method dispatch, generic monomorphization) would be added as
the GitLanguage's `deep_sync` (see plugin.py). Registered as the `rust` GitLanguage for the
multi-language endpoint (core/ingest.py) in both ingest_repository (git) and ingest_directory
(local)."""

from __future__ import annotations

import itertools
import logging
import time
from dataclasses import dataclass

from auryn.core.parallel import parse_blobs_ordered
from auryn.core.graph_model import GraphNode
from auryn.core.graph_sync import stream_apply
from auryn.core.registry import LanguageIngestResult
from auryn.ingestors.rust import cargo, resolution
from auryn.ingestors.rust.file_parser import parse_source
from auryn.ingestors.rust.ids import package_id, project_id
from auryn.ingestors.rust.plugin import EXCLUDED_DIR_NAMES as _EXCLUDED_DIR_NAMES
from auryn.ingestors.rust.plugin import FILE_SUFFIXES as _FILE_SUFFIXES
from auryn.ingestors.rust.schema import NODE_SCHEMAS

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


# Two-phase depth (mirrors C#/Python): the fast tree-sitter pass stamps the Project `depth="fast"`,
# the deep rust-analyzer pass `depth="deep"`. A re-run short-circuits only when the commit matches AND
# the existing depth is at least the requested one -- so a fast re-run never downgrades a deep graph,
# but the deep phase still runs right after the fast one at the same commit.
_DEPTH_RANK = {None: 0, "": 0, "fast": 1, "deep": 2}


def _project_node(proj_id: str, project_name: str, repo_uri: str, target_sha: str, ref: str | None, depth: str) -> GraphNode:
    return GraphNode(
        "Project", {"id": proj_id},
        {"name": project_name, "repo_uri": repo_uri, "commit_sha": target_sha, "ref": ref, "depth": depth},
    )


def sync_at(writer, source, target_sha: str, project_name: str, repo_uri: str, ref: str | None,
            engine: str = "treesitter") -> SyncStats:
    """Recompute the whole Rust subgraph at `target_sha` and delta it onto the graph. `source` is
    duck-typed (GitRepoCache or LocalDirectorySource). `engine`: "treesitter" (fast, blob-streamed) or
    "rust_analyzer" (deep, SCIP over an on-disk cargo checkout, falling back to tree-sitter if the tool
    or a buildable crate is unavailable)."""
    started = time.monotonic()
    proj_id = project_id(project_name)
    requested_depth = "deep" if engine == "rust_analyzer" else "fast"

    writer.setup_schema(NODE_SCHEMAS)  # brings the graph key into existence before the first read
    current_sha = writer.get_property("Project", {"id": proj_id}, "commit_sha")
    current_depth = writer.get_property("Project", {"id": proj_id}, "depth")
    if current_sha == target_sha and _DEPTH_RANK.get(current_depth, 0) >= _DEPTH_RANK[requested_depth]:
        return SyncStats(commit_sha=target_sha, elapsed_seconds=time.monotonic() - started)

    # Incremental resync (ADR 0005, authored-shared engine) -- fast engine only; the deep pass always
    # fully re-analyzes (rust-analyzer is whole-crate), like C#'s Roslyn. When a prior graph exists at a
    # different, reachable commit, move it by processing only the git diff + its graph-derived blast
    # radius instead of recomputing the whole tree. Any failure falls back to the full recompute below.
    if engine == "treesitter" and current_sha and current_sha != target_sha:
        incr = _route(writer, source, target_sha, ref, current_sha,
                      project_name=project_name, repo_uri=repo_uri)
        if incr is not None:
            _reconcile_cargo(writer, proj_id, source, target_sha)
            return _finish(writer, proj_id, source, incr)

    entries = [
        (path, blob_sha)
        for suffix in _FILE_SUFFIXES
        for path, blob_sha in source.list_files(target_sha, suffix, set(_EXCLUDED_DIR_NAMES))
    ]
    files = parse_blobs_ordered(
        entries, source.read_blob, parse_source,
        preload=["auryn.ingestors.rust.file_parser"], logger=logger,
    )

    if engine == "rust_analyzer":
        is_upgrade = current_sha == target_sha and current_depth == "fast"
        deep = _deep_sync_at(writer, source, target_sha, files, proj_id, project_name, repo_uri, ref,
                             started, is_upgrade=is_upgrade)
        if deep is not None:
            return deep
        # rust-analyzer unavailable / crate not analyzable -> fall through to the tree-sitter fast pass.

    project_node = _project_node(proj_id, project_name, repo_uri, target_sha, ref, "fast")

    def make_target():  # re-iterable: stream_apply runs it once for nodes, once for edges
        return itertools.chain([project_node], resolution.resolve(files, proj_id))

    result = _apply(writer, proj_id, make_target, target_sha, started, files_parsed=len(files))
    _reconcile_cargo(writer, proj_id, source, target_sha)
    return _finish(writer, proj_id, source, result)


def _deep_sync_at(writer, source, target_sha: str, files, proj_id: str, project_name: str, repo_uri: str,
                  ref: str | None, started: float, *, is_upgrade: bool) -> "SyncStats | None":
    """Deep pass: resolve CALLS precisely via rust-analyzer's SCIP index, layered over the fast pass's
    structural subgraph. Reuses the fast parse for all structure (same node ids), replaces the name-based
    CALLS with `resolution:"resolved"` edges, and zeroes `unresolved_calls`. Returns None (caller falls
    back to tree-sitter) if rust-analyzer can't run. `diff_extern_edges=False` on an upgrade preserves the
    fast pass's extern:// placeholders (deep can't author cross-repo edges; reconcile heals them)."""
    from auryn.ingestors.rust import rust_analyzer

    try:
        with source.working_tree(target_sha) as workdir:  # on-disk cargo checkout rust-analyzer needs
            scip_bytes = rust_analyzer.run_scip(str(workdir))
    except rust_analyzer.RustAnalyzerUnavailable as exc:
        logger.warning("rust deep pass unavailable (%s); falling back to tree-sitter", exc)
        return None

    locations = resolution.callable_locations(files, proj_id)
    deep_edges, metrics = rust_analyzer.resolve_calls(scip_bytes, locations)
    project_node = _project_node(proj_id, project_name, repo_uri, target_sha, ref, "deep")

    def make_target():
        items: list = [project_node]
        for item in resolution.resolve(files, proj_id):
            if _is_calls_edge(item):  # deep owns CALLS; fast extern:// edges survive via diff_extern_edges=False
                continue
            items.append(_zero_unresolved(item) if _is_callable_node(item) else item)
        items.extend(deep_edges)
        return iter(items)

    result = _apply(writer, proj_id, make_target, target_sha, started, files_parsed=len(files),
                    diff_extern_edges=not is_upgrade)
    logger.info("rust deep: %d resolved / %d external CALLS over %d callables",
                metrics["resolved"], metrics["external"], len(locations))
    _reconcile_cargo(writer, proj_id, source, target_sha)
    return _finish(writer, proj_id, source, result)


def _is_calls_edge(item) -> bool:
    return type(item).__name__ == "GraphEdge" and item.rel_type == "CALLS"


def _is_callable_node(item) -> bool:
    return type(item).__name__ == "GraphNode" and item.label in ("Function", "Method")


def _zero_unresolved(node: GraphNode) -> GraphNode:
    """Re-emit a callable node with `unresolved_calls=0`: the deep pass resolved its calls precisely, so
    the fast pass's incompleteness count (#792) no longer applies."""
    return GraphNode(node.label, node.key, {**node.properties, "unresolved_calls": 0})


def _route(writer, source, target_sha, ref, current_sha, *, project_name, repo_uri) -> "SyncStats | None":
    """Attempt an incremental resync via the shared engine's git routing; return SyncStats or None
    to fall back to the full recompute. RUST_SPEC sets `provenance_rels`, so route_incremental
    dispatches to the authored-shared engine (incremental_sync_authored)."""
    from auryn.core.incremental_scoped import route_incremental
    from auryn.ingestors.rust.incremental import RUST_SPEC

    s = route_incremental(
        writer, RUST_SPEC, source, target_sha, ref, current_sha,
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


def _apply(writer, proj_id: str, make_target, target_sha: str, started: float, files_parsed: int,
           *, diff_extern_edges: bool = True) -> SyncStats:
    # Cross-repo reconciliation (ADR 0004): fold placeholder<->definer pairs among the linkages
    # this ingest touched -- targeted index seeks, no scan. No-op unless the graph holds >1 repo.
    from auryn.core.linkage import reconcile_linkages

    # stream_apply writes the delta without materialising the target graph, and returns the
    # linkage strings collected inline (see graph_sync.stream_apply / core.linkage). The deep pass
    # passes diff_extern_edges=False so it doesn't prune the fast pass's extern:// placeholder CALLS
    # (it can't author cross-repo edges; reconcile_linkages heals them when the definer is ingested).
    labels = [s.label for s in NODE_SCHEMAS]
    stats, reals, placeholders = stream_apply(writer, proj_id, labels, make_target,
                                              diff_extern_edges=diff_extern_edges)
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


_CARGO_MANIFEST = "Cargo.toml"


def _reconcile_cargo(writer, proj_id: str, source, target_sha: str) -> None:
    """Layer the manifest-only facts onto the path-derived Cargo tier (issue #5): per-Package
    edition / version / canonical name, and crate-to-crate DEPENDS_ON from `[dependencies]`.

    Runs in BOTH the full and incremental paths, deterministically from the `Cargo.toml` set at
    `target_sha` -- so the differential oracle's `incremental == full` holds. This is where manifest
    data enters at all: the delta engine's per-file callbacks get only a path (no source), so they
    cannot read a manifest; `sync_at` (which has `source`) does, here. Package/Crate NODES are
    authored by resolution (path-derived) and GC'd by the aggregation refcount -- this step only
    enriches existing ones (a per-property SET, never a replace, so resolution's own props survive)
    and reconciles the dependency edges."""
    manifests = []
    for rel, blob in source.list_files(target_sha, _CARGO_MANIFEST, set(_EXCLUDED_DIR_NAMES)):
        manifest_dir = rel[: -len(_CARGO_MANIFEST)].rstrip("/")
        manifests.append(cargo.parse_manifest(manifest_dir, source.read_blob(blob)))
    model = cargo.build_cargo_model(manifests)

    for p in model.packages:
        writer.query(
            "MATCH (pkg:Package {id: $id}) SET pkg.name = $name, pkg.version = $version, "
            "pkg.edition = $edition, pkg.is_workspace_root = $wsroot, "
            "pkg.external_dependencies = $ext",
            {"id": package_id(proj_id, p.dir), "name": p.name, "version": p.version,
             "edition": p.edition, "wsroot": p.is_workspace_root, "ext": p.external_dependencies},
        )

    # Reconcile Package->Package DEPENDS_ON: MERGE the desired set, delete any stale edge. Scoped to
    # THIS project's Package endpoints, so resolution's unrelated DEPENDS_ON (callable/field -> type)
    # is never touched. MERGE no-ops when a dependency package has no .rs files (so no Package node).
    desired = {(package_id(proj_id, a), package_id(proj_id, b)) for a, b in model.depends_on}
    existing = writer.ro_query(
        "MATCH (a:Package)-[:DEPENDS_ON]->(b:Package) WHERE a.id STARTS WITH $pre RETURN a.id, b.id",
        {"pre": proj_id + "/package"},
    ).result_set
    for a, b in existing:
        if (str(a), str(b)) not in desired:
            writer.query(
                "MATCH (a:Package {id: $a})-[r:DEPENDS_ON]->(b:Package {id: $b}) DELETE r",
                {"a": str(a), "b": str(b)},
            )
    for a, b in sorted(desired):
        writer.query(
            "MATCH (a:Package {id: $a}), (b:Package {id: $b}) "
            "MERGE (a)-[r:DEPENDS_ON]->(b) SET r.kind = 'cargo', r.resolution = 'name'",
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
    engine: str = "treesitter",
) -> LanguageIngestResult:
    """GitLanguage adapter for the multi-language endpoint (Rust has no solution/cache knobs --
    accepted for the shared interface and ignored). `engine` selects the fast or deep pass."""
    s = sync_at(writer, source, target_sha, project_name=project_name, repo_uri=repo_uri, ref=ref,
                engine=engine)
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


def treesitter_language_sync(writer, source, target_sha, **kwargs) -> LanguageIngestResult:
    """Fast-phase GitLanguage `sync` thunk (the default engine)."""
    return _git_language_sync(writer, source, target_sha, engine="treesitter", **kwargs)


def rust_analyzer_language_sync(writer, source, target_sha, **kwargs) -> LanguageIngestResult:
    """Deep-phase GitLanguage `deep_sync` thunk (rust-analyzer / SCIP, degrading to the fast pass)."""
    return _git_language_sync(writer, source, target_sha, engine="rust_analyzer", **kwargs)


# The GitLanguage is registered by the import-light plugin.py (the entry-point target), whose lazy
# `sync`/`deep_sync` thunks call the adapters above -- so importing this engine module (and, for the
# deep phase, rust-analyzer) is deferred until a source actually contains Rust files.
