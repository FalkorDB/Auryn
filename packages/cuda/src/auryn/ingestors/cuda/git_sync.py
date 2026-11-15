"""Git-tracked (and directory) CUDA ingestion: recompute the project's CUDA subgraph at a target
commit/tree and diff it onto the graph (graph_sync.stream_apply) -- the same recompute-and-delta
model as the C/C++/C#/Java/Python ingestors.

CUDA is a strict superset of C++, so the pipeline is the C++ one: tree-sitter-cuda streams each
`.cu`/`.cuh` blob -> CudaRawFile (C++ structure via the shared engine + the CUDA execution-space /
launch side-cars) -> resolution.resolve (the shared C++ resolver plus the execution_space property
and LAUNCHES edges).

v1 is FULL-RECOMPUTE: every sync rebuilds the subgraph and stream_apply writes only the delta
(unchanged files are a no-op, re-ingest is cheap and correct). An incremental blast-radius engine
(a CUDA_SPEC over core.incremental_scoped, mirroring CPP_SPEC, with the execution_space/LAUNCHES
augmentation folded into resolve_incremental) is the planned follow-up; a deep libclang/nvcc pass
would slot into the plugin's `deep_sync`.

Known v1 limitations (documented, follow-ups):
  * Deleting the LAST .cu/.cuh file leaves the CUDA subgraph in place -- the multi-language endpoint
    skips a language with zero detected files, so no recompute runs. Shared with every other
    full-recompute-only language (only c/cpp have bespoke zero-file cleanup, for the header-flip
    case); it resolves for free once CUDA gains incremental delta-sync, which removes deleted files
    from the git diff. Re-ingesting with the files present, or forgetting the project, clears it.
  * A templated kernel launch (`reduce<T><<<...>>>`) gets a LAUNCHES edge but may lack the parallel
    CALLS edge, because the shared C++ call extractor does not yet capture templated callees (a
    pre-existing cpp limitation that also drops plain templated C++ calls) -- see resolution.py."""

from __future__ import annotations

import itertools
import logging
import time
from dataclasses import dataclass

from auryn.core.graph_model import GraphNode
from auryn.core.graph_sync import stream_apply
from auryn.core.parallel import parse_blobs_ordered
from auryn.core.registry import LanguageIngestResult
from auryn.ingestors.cuda import resolution
from auryn.ingestors.cuda.file_parser import parse_source
from auryn.ingestors.cuda.ids import project_id
from auryn.ingestors.cuda.plugin import EXCLUDED_DIR_NAMES as _EXCLUDED_DIR_NAMES
from auryn.ingestors.cuda.plugin import FILE_SUFFIXES as _FILE_SUFFIXES
from auryn.ingestors.cuda.schema import NODE_SCHEMAS

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
    """Recompute the whole CUDA subgraph at `target_sha` and delta it onto the graph. `source` is
    duck-typed (GitRepoCache or LocalDirectorySource): only list_files/read_blob are used."""
    started = time.monotonic()
    proj_id = project_id(project_name)

    writer.setup_schema(NODE_SCHEMAS)  # brings the graph key into existence before the first read
    current_sha = writer.get_property("Project", {"id": proj_id}, "commit_sha")
    if current_sha == target_sha:
        return SyncStats(commit_sha=target_sha, elapsed_seconds=time.monotonic() - started)

    entries = [
        (path, blob_sha)
        for suffix in _FILE_SUFFIXES
        for path, blob_sha in source.list_files(target_sha, suffix, set(_EXCLUDED_DIR_NAMES))
    ]
    files = parse_blobs_ordered(
        entries, source.read_blob, parse_source,
        preload=["auryn.ingestors.cuda.file_parser"], logger=logger,
    )

    project_node = GraphNode(
        "Project", {"id": proj_id},
        {"name": project_name, "repo_uri": repo_uri, "commit_sha": target_sha, "ref": ref},
    )

    def make_target():  # re-iterable: stream_apply runs it once for nodes, once for edges
        return itertools.chain([project_node], resolution.resolve(files, proj_id))

    return _apply(writer, proj_id, make_target, target_sha, started, files_parsed=len(files))


def _apply(writer, proj_id: str, make_target, target_sha: str, started: float, files_parsed: int) -> SyncStats:
    # Cross-repo reconciliation (ADR 0004): fold placeholder<->definer pairs among the linkages this
    # ingest touched -- targeted index seeks, no scan. No-op unless the graph holds >1 repo.
    from auryn.core.linkage import reconcile_linkages

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
    """GitLanguage adapter for the multi-language endpoint (CUDA has no solution/cache knobs --
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


# The GitLanguage is registered by the import-light plugin.py (the entry-point target), whose lazy
# `sync` thunk calls `_git_language_sync` above -- so importing this tree-sitter engine module is
# deferred until a source actually contains CUDA files.
