"""Git-tracked (and directory) C++ ingestion: moves a project's C++ subgraph to a target
commit/tree by fully recomputing the target state and diffing it against the graph
(graph_sync.compute_delta) -- the same recompute-and-delta model as the C/C#/Java/Python
ingestors (resolution is global -- the namespace-wide type/method/function index and the
out-of-line member-definition wiring -- so a file-level diff can't tell which untouched files a
change affects).

Fast engine only for now: tree-sitter-cpp streams each C++ blob (no disk materialization, no
build config) -> RawFile -> resolution.resolve(). A deep libclang pass (template instantiation,
overload resolution, ADL, precise header linking, cross-TU calls) would be added as the
GitLanguage's `deep_sync` (see plugin.py). Registered as the `cpp` GitLanguage for the
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
from auryn.ingestors.cpp import resolution
from auryn.ingestors.cpp.file_parser import parse_source
from auryn.ingestors.cpp.ids import project_id
from auryn.ingestors.cpp.plugin import EXCLUDED_DIR_NAMES as _EXCLUDED_DIR_NAMES
from auryn.core.cpp_headers import HEADER_SUFFIX, is_cpp_header_blob
from auryn.ingestors.cpp.plugin import FILE_SUFFIXES as _FILE_SUFFIXES
from auryn.ingestors.cpp.schema import NODE_SCHEMAS

logger = logging.getLogger(__name__)

# Provenance-format marker stamped on the Project node once a graph has been built with the
# File-[:DEFINES|DECLARES]->entity provenance the authored incremental engine reads (ADR 0005). A C++
# graph built by an OLDER Auryn (full-recompute only, NO provenance edges) lacks it; the incremental
# route is gated on this marker so such a legacy graph does one full recompute first (which stamps it),
# instead of taking the incremental path against a graph the engine can't scope. Bump on a format change.
_DELTA_FORMAT = 1


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
    """Recompute the whole C++ subgraph at `target_sha` and delta it onto the graph. `source`
    is duck-typed (GitRepoCache or LocalDirectorySource): only list_files/read_blob used."""
    started = time.monotonic()
    proj_id = project_id(project_name)

    writer.setup_schema(NODE_SCHEMAS)  # brings the graph key into existence before the first read
    current_sha = writer.get_property("Project", {"id": proj_id}, "commit_sha")
    if current_sha == target_sha:
        return SyncStats(commit_sha=target_sha, elapsed_seconds=time.monotonic() - started)

    # Incremental resync (ADR 0005, authored-shared engine): when a prior graph already exists at a
    # different, reachable commit, move it by processing only the git diff + its graph-derived blast
    # radius (co-contributor / co-member / reverse-dep closures, provenance-scoped read-back, refcount
    # delete-or-demote) instead of recomputing the whole tree -- see incremental.py. Any failure (old
    # commit unreachable, non-diffable source, plain dir) falls back to the full recompute below.
    # Only route incremental when the existing graph carries the provenance format (a legacy graph built
    # by an older Auryn has commit_sha but no DEFINES/DECLARES provenance -> full recompute re-stamps it).
    has_provenance = writer.get_property("Project", {"id": proj_id}, "delta_format") == _DELTA_FORMAT
    if current_sha and current_sha != target_sha and has_provenance:
        incr = _route(writer, source, target_sha, ref, current_sha,
                      project_name=project_name, repo_uri=repo_uri)
        if incr is not None:
            return _finish(writer, proj_id, source, incr)

    entries = [
        (path, blob_sha)
        for suffix in _FILE_SUFFIXES
        for path, blob_sha in source.list_files(target_sha, suffix, set(_EXCLUDED_DIR_NAMES))
    ]
    # `.h` is not in C++'s suffix list (it's C's by default), but a `.h` with C++ constructs IS a C++
    # header -- the dominant case in real C++ (protobuf/LLVM/Chromium use `.h`). Claim those here so the
    # class declared in the header joins its `.cc` definition under one `cpp://` node; the C ingestor
    # skips the same set (see core.cpp_headers / c.git_sync).
    # KNOWN RESIDUAL (narrow; PR #16 review): the save-watcher derives its trigger extensions from
    # registered file_suffixes (core/watcher.py). `.h` reaches it via the C ingestor's suffix list, so a
    # normal (C + C++) install resyncs on `.h` edits. In a C++-ONLY install (no auryn-c), `.h` is in no
    # suffix list, so a save-triggered watcher won't resync an edited C++ `.h` until a manual/commit sync.
    # The robust fix (content-gated `.h` as a first-class C++ suffix) lands with the incremental `.h` glue.
    entries += [
        (path, blob_sha)
        for path, blob_sha in source.list_files(target_sha, HEADER_SUFFIX, set(_EXCLUDED_DIR_NAMES))
        if is_cpp_header_blob(source.read_blob(blob_sha))
    ]
    files = parse_blobs_ordered(
        entries, source.read_blob, parse_source,
        preload=["auryn.ingestors.cpp.file_parser"], logger=logger,
    )

    project_node = GraphNode(
        "Project", {"id": proj_id}, {"name": project_name, "repo_uri": repo_uri, "commit_sha": target_sha, "ref": ref}
    )

    def make_target():  # re-iterable: stream_apply runs it once for nodes, once for edges
        return itertools.chain([project_node], resolution.resolve(files, proj_id))

    return _finish(writer, proj_id, source,
                   _apply(writer, proj_id, make_target, target_sha, started, files_parsed=len(files)))


def _route(writer, source, target_sha, ref, current_sha, *, project_name, repo_uri) -> "SyncStats | None":
    """Attempt an incremental resync via the shared engine's git routing (working-tree or bare-clone
    producer); return SyncStats or None to fall back to the full recompute. C++'s CPP_SPEC sets
    `provenance_rels`, so route_incremental dispatches to the authored-shared engine."""
    from auryn.core.incremental_scoped import route_incremental
    from auryn.ingestors.cpp.incremental import CPP_SPEC

    s = route_incremental(
        writer, CPP_SPEC, source, target_sha, ref, current_sha,
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
    """After any non-short-circuit sync, record the working-tree base for the next save-triggered
    incremental resync (a no-op for git/plain-dir sources)."""
    from auryn.core.incremental_scoped import stamp_worktree
    from auryn.core.local_source import WorkingTreeSource

    if isinstance(source, WorkingTreeSource):
        # Stamp `.h` too (not just _FILE_SUFFIXES): since the incremental engine now claims C++-content
        # `.h` headers, a dirty header edit must be remembered as a dirty path -- otherwise reverting an
        # uncommitted `.h` edit back to HEAD leaves no git diff AND no stamped path, so the next
        # save-triggered resync would never reprocess it and the graph would keep the edited version.
        stamp_worktree(writer, proj_id, source, _FILE_SUFFIXES + (HEADER_SUFFIX,))
    # Mark the graph as carrying the provenance format (gates the incremental route; see _DELTA_FORMAT).
    # A side-channel prop set after the Project node is written -- never part of its diffed fingerprint.
    writer.query("MATCH (p:Project {id: $id}) SET p.delta_format = $v", {"id": proj_id, "v": _DELTA_FORMAT})
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
    """GitLanguage adapter for the multi-language endpoint (C++ has no solution/cache knobs --
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
# module is deferred until a source actually contains C++ files.
