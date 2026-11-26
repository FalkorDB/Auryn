"""The multi-language ingestion endpoint -- source-agnostic.

Two entry points share one core (`_ingest_from_source`): `ingest_repository` for a
git repo (a path or URI git understands) and `ingest_directory` for a plain local
directory (no git). Both prepare a "source" and a `target_sha`, then ask every
registered file-based language (see core/registry.py's GitLanguage /
discover_git_languages) whether it has files there and, if so, ingest them into the
graph. One source, however many languages -- the caller doesn't name a language, and
adding support for a new one never touches this module.

Nothing below is git-specific: the shared core consumes the source through only
list_files / read_blob / working_tree, which both GitRepoCache (git_source.py) and
LocalDirectorySource (local_source.py) implement. fetch/resolve-a-ref (git) and
hash-the-tree (local) happen in the respective entry point before the core runs.

Well-logged by design: every milestone (source prepared, per-language begin and end,
overall finish) goes to the `auryn.ingest` logger at INFO. That reaches whoever
launched the process (the MCP server logs to stderr, safely separate from its stdout
JSON-RPC stream); the *calling* agent, which over MCP only ever sees a tool's return
value, gets the same information as the structured RepoIngestReport.

A language that raises mid-ingestion is logged and recorded on its own
LanguageReport with an `error`, and the remaining languages still run -- one bad
subgraph never aborts the whole source.
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from auryn.core.app_config import effective_disabled_hooks
from auryn.core.cpp_headers import cpp_header_paths
from auryn.core.git_source import GitRepoCache, repos_cache_dir
from auryn.core.graph_client import GraphWriter
from auryn.core.local_source import LocalDirectorySource, WorkingTreeSource
from auryn.core.registry import (
    GitLanguage,
    LanguageIngestResult,
    discover_git_languages,
    discover_graph_enrichments,
    select_enrichments,
)

logger = logging.getLogger("auryn.ingest")

# Either backing source the pipeline can run over: a git repo (GitRepoCache) or a
# plain local directory (LocalDirectorySource). Both expose the same small surface --
# list_files / read_blob / working_tree -- that every language ingestor and enrichment
# consumes, so the shared core below is duck-typed over the two.
Source = GitRepoCache | LocalDirectorySource

# How the *deep* phase of a two-phase (dynamic) language runs, once the fast phase has made the
# graph queryable. A language declares it's two-phase by registering a GitLanguage with a `deep_sync`
# (see core/registry.py): Python (ast->Jedi), C# (tree-sitter->Roslyn), Rust (tree-sitter->SCIP).
#   "async"      -- DEFAULT. Return as soon as the fast graph is written and deepen it in a DETACHED
#                   daemon (core/deepen) that survives this process exiting -- so the graph is
#                   queryable at fast-pass speed and deepens in the background even from a one-shot
#                   CLI. Falls back to "inline" when there's no re-ingestable local dir (remote-URI
#                   ingest) or the backend is an in-process embedded DB a daemon can't reach.
#   "inline"     -- run the deep upgrade synchronously before returning (the graph is fully deep when
#                   the call completes; for CI/batch that must not query until resolution is done).
#   "background" -- deepen in a daemon THREAD on a sibling writer -- long-lived processes only (a
#                   one-shot CLI would kill the thread; prefer "async").
#   "off"        -- fast phase only; leave the graph shallow.
DEEP_MODES = ("inline", "background", "async", "off")

# Background deep passes started by this process (daemon threads). join_deep_phase() awaits
# them -- optional for a server, used by tests and any caller that must not exit early. Each thread
# carries its own `.deep_outcome` {"lang","error"} so a caller can tell which of ITS passes failed
# (deep_failures()) without a shared mutable list that a concurrent ingest or the embed-waiter could
# consume -- see _spawn_deep / #857.
_deep_threads: list[threading.Thread] = []
# Guards _deep_threads so a spawn's publish+start is atomic with join_deep_phase()'s snapshot+prune
# -- otherwise a concurrent join could prune a just-appended, not-yet-started thread (is_alive() is
# False) or try to join an unstarted one (RuntimeError), losing track of a running deep writer (#857).
_deep_lock = threading.Lock()

# Thread-local collector for deep_failure_scope(): while a scope is active on THIS thread, every deep
# thread _spawn_deep starts (synchronously, on this thread) is appended here, so the scope joins and
# reads exactly its own ingest's passes -- race-free across concurrent ingests, and immune to the
# embed-waiter pruning the shared _deep_threads list (#857).
_tls = threading.local()


def _build_filter(include_dirs, exclude_dirs, include_suffixes, exclude_suffixes):
    """Assemble the optional allow/deny SourceFilter and log it when non-empty."""
    from auryn.core.source_filter import SourceFilter

    f = SourceFilter.build(
        include_dirs=include_dirs, exclude_dirs=exclude_dirs,
        include_suffixes=include_suffixes, exclude_suffixes=exclude_suffixes,
    )
    if not f.is_empty():
        logger.info("Source filter: %s", f.describe())
    return f

# Extensions belonging to a language we can't yet ingest *from files*, reported (not
# ingested) so a repo ingestion is honest about what it left untouched rather than
# looking like it covered the whole repo. Only reported for extensions no registered
# language already claims. Not exhaustive -- just high-signal ones. Vendored trees
# are skipped so a node_modules doesn't drown the signal.
# (.ts/.tsx/.js/.jsx, .java, .go, .rs, .php and .jl are now claimed by their languages, so
# they're dropped here -- _scan_unsupported skips any extension a registered language claims,
# but keeping the catalog in step with what's actually supported keeps it honest.)
_KNOWN_UNSUPPORTED: dict[str, str] = {
    ".razor": "blazor",
    ".rb": "ruby",
}
_UNSUPPORTED_SCAN_EXCLUDES = {"node_modules", "bin", "obj", "dist", "build", "packages", "vendor"}


@dataclass(frozen=True)
class LanguageReport:
    """One language's outcome within a repo ingestion. `result` is the fast (or, for a
    single-phase language, the only) pass; it is None exactly when `error` is set (the fast
    pass raised). `files_detected` is filled either way.

    For a two-phase language, `deep_status` records what happened to the deep upgrade
    ("inline" done now, "background" running in a daemon thread, "off"/"skipped", or "error")
    and `deep_result` carries its delta when it ran synchronously (None for background/off)."""

    language: str
    files_detected: int
    result: LanguageIngestResult | None
    error: str | None = None
    deep_result: LanguageIngestResult | None = None
    deep_status: str | None = None

    def to_dict(self) -> dict:
        out: dict[str, object] = {"language": self.language, "files_detected": self.files_detected}
        if self.error is not None:
            out["error"] = self.error
        if self.result is not None:
            out.update(
                nodes_added=self.result.nodes_added,
                nodes_updated=self.result.nodes_updated,
                nodes_removed=self.result.nodes_removed,
                edges_added=self.result.edges_added,
                edges_updated=self.result.edges_updated,
                edges_removed=self.result.edges_removed,
                files_parsed=self.result.files_parsed,
                files_cached=self.result.files_cached,
                elapsed_seconds=round(self.result.elapsed_seconds, 3),
            )
            if self.result.broken_files:  # files that failed to parse; their last-good graph was kept
                out["broken_files"] = self.result.broken_files
        if self.deep_status is not None:
            out["deep_status"] = self.deep_status
            if self.deep_result is not None:
                out["deep"] = {
                    "nodes_updated": self.deep_result.nodes_updated,
                    "nodes_added": self.deep_result.nodes_added,
                    "edges_added": self.deep_result.edges_added,
                    "edges_updated": self.deep_result.edges_updated,
                    "elapsed_seconds": round(self.deep_result.elapsed_seconds, 3),
                }
        return out


@dataclass(frozen=True)
class RepoIngestReport:
    repo_uri: str
    project_name: str
    solution_name: str | None
    commit_sha: str
    ref: str | None
    languages: list[LanguageReport]
    unsupported_detected: list[dict]
    elapsed_seconds: float
    enrichments: dict = field(default_factory=dict)
    auto_installed: list[str] = field(default_factory=list)  # auryn-<lang> packages pulled on demand this run

    def _totals(self) -> dict[str, int]:
        keys = (
            "nodes_added",
            "nodes_updated",
            "nodes_removed",
            "edges_added",
            "edges_updated",
            "edges_removed",
        )
        totals = {k: 0 for k in keys}
        for report in self.languages:
            if report.result is None:
                continue
            for k in keys:
                totals[k] += getattr(report.result, k)
        return totals

    def to_dict(self) -> dict:
        return {
            "repo_uri": self.repo_uri,
            "project_name": self.project_name,
            "solution_name": self.solution_name,
            "commit_sha": self.commit_sha,
            "ref": self.ref,
            "languages_ingested": [r.language for r in self.languages if r.result is not None],
            "languages": [r.to_dict() for r in self.languages],
            "unsupported_detected": self.unsupported_detected,
            "auto_installed": self.auto_installed,
            "enrichments": self.enrichments,
            "totals": self._totals(),
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "note": (
                "Project ids are derived from the repo/name; if you ingest several "
                "repos into one graph, ensure no project name is a literal string "
                "prefix of another (e.g. 'App.WEB' vs 'App.Api' is fine; a bare "
                "'App' alongside 'App.WEB' is not) -- see the graph_client prefix "
                "caveat."
            ),
        }


def ingest_repository(
    writer: GraphWriter,
    repo_uri: str,
    *,
    ref: str | None = None,
    project_name: str | None = None,
    solution_name: str | None = None,
    cache_root: Path | None = None,
    auto_install: bool | None = None,
    deep: str = "async",
    embed: str | None = None,
    include_dirs: tuple[str, ...] = (),
    exclude_dirs: tuple[str, ...] = (),
    include_suffixes: tuple[str, ...] = (),
    exclude_suffixes: tuple[str, ...] = (),
    enrichment_only: tuple[str, ...] = (),
    enrichment_skip: tuple[str, ...] = (),
) -> RepoIngestReport:
    """Ingest every supported language found in the git repo at `repo_uri` (a path
    or any URI git understands) into `writer`'s graph, moving each language's
    subgraph to `ref` (default: the repo's default branch). `project_name` defaults
    to the repo's own name. `deep` controls two-phase languages' deep upgrade
    ("inline"|"background"|"off"; see DEEP_MODES). `embed` overrides how the post-ingest
    embedding pass runs ("inline"|"background"|"off"; None -> resolve from env/config, default
    background -- see embeddings.embedder.embed_mode); callers on an ad-hoc connection a separate
    process couldn't reach (serverless/quickstart, explicit --host) must pass "inline". The
    include_/exclude_ dir and suffix lists are an optional allow/deny filter (see
    core/source_filter.SourceFilter). See the module docstring for logging/error behavior."""
    started = time.monotonic()
    project_name = project_name or _derive_project_name(repo_uri)
    logger.info("Ingesting repository %s (ref=%s) as project %r", repo_uri, ref or "<default>", project_name)

    file_filter = _build_filter(include_dirs, exclude_dirs, include_suffixes, exclude_suffixes)
    repo = GitRepoCache(repo_uri, repos_cache_dir(cache_root), file_filter=file_filter)
    repo.fetch()
    target_sha = repo.resolve_ref(ref)
    logger.info("Resolved ref %s to commit %s", ref or "<default branch>", target_sha)

    return _ingest_from_source(
        writer,
        repo,
        target_sha,
        source_uri=repo_uri,
        project_name=project_name,
        solution_name=solution_name,
        ref=ref,
        cache_root=cache_root,
        started=started,
        auto_install=auto_install,
        deep=deep,
        # A git repo is pinned to `ref` (a bare-clone cache, not the working tree); the daemon's
        # working-tree replay would index HEAD + uncommitted changes instead, so never async here.
        async_eligible=False,
        embed=embed,
        enrichment_only=enrichment_only,
        enrichment_skip=enrichment_skip,
    )


def ingest_directory(
    writer: GraphWriter,
    path: str,
    *,
    project_name: str | None = None,
    solution_name: str | None = None,
    cache_root: Path | None = None,
    auto_install: bool | None = None,
    deep: str = "async",
    embed: str | None = None,
    include_dirs: tuple[str, ...] = (),
    exclude_dirs: tuple[str, ...] = (),
    include_suffixes: tuple[str, ...] = (),
    exclude_suffixes: tuple[str, ...] = (),
    enrichment_only: tuple[str, ...] = (),
    enrichment_skip: tuple[str, ...] = (),
) -> RepoIngestReport:
    """Ingest every supported language found under the local directory `path`
    (recursively, all nested subfolders) into `writer`'s graph -- no git required.
    `project_name` defaults to the directory's own name. The include_/exclude_ dir and
    suffix lists are an optional allow/deny filter (see core/source_filter.SourceFilter).

    The same delta machinery as `ingest_repository` applies: content is keyed by a
    hash of each file's bytes and the whole tree by a hash over all files (standing in
    for a commit sha), so re-ingesting an unchanged directory is a no-op and a changed
    file produces a precise delta. Raises ValueError if `path` is missing or not a
    directory. See the module docstring for logging/error behavior."""
    started = time.monotonic()
    root = Path(path).expanduser()
    if not root.exists():
        raise ValueError(f"path does not exist: {path!r}")
    if not root.is_dir():
        raise ValueError(f"not a directory: {path!r}")
    root = root.resolve()
    source_uri = str(root)
    project_name = project_name or root.name or "repo"
    logger.info("Ingesting local directory %s as project %r", source_uri, project_name)

    file_filter = _build_filter(include_dirs, exclude_dirs, include_suffixes, exclude_suffixes)
    # A git working tree gets the WorkingTreeSource (git-driven change detection -> incremental
    # resync for the file-save / project-save trigger); a plain directory falls back to the
    # content-hash LocalDirectorySource (full resync). Both expose the same source surface.
    if WorkingTreeSource.is_git_worktree(root):
        source: Source = WorkingTreeSource(root, file_filter=file_filter)
        logger.info("Local directory %s is a git working tree -- incremental resync enabled", source_uri)
    else:
        source = LocalDirectorySource(root, file_filter=file_filter)
    target_sha = source.tree_sha()
    logger.info("Hashed local directory tree to %s", target_sha)

    return _ingest_from_source(
        writer,
        source,
        target_sha,
        source_uri=source_uri,
        project_name=project_name,
        solution_name=solution_name,
        ref=None,
        cache_root=cache_root,
        started=started,
        auto_install=auto_install,
        deep=deep,
        # async's daemon replays `ingest_directory(local_dir, deep="inline")` with DEFAULT filters and
        # no solution wrapper -- so it faithfully reproduces this ingest only when those are default.
        async_eligible=file_filter.is_empty() and not solution_name,
        embed=embed,
        enrichment_only=enrichment_only,
        enrichment_skip=enrichment_skip,
    )


def _source_local_dir(source: Source, source_uri: str) -> str | None:
    """Best-effort local working directory of a source, for matching a RepoRegistration
    (per-repo hook disables). A local directory / git working tree resolves to its root; a
    remote-URI git ingest (no local checkout) returns None, so only install-wide disables apply."""
    root = getattr(source, "_root", None)
    if root is not None:
        return str(Path(root).resolve())
    p = Path(source_uri).expanduser()
    return str(p.resolve()) if p.is_dir() else None


def run_post_hooks(
    writer: GraphWriter,
    source: Source,
    target_sha: str,
    *,
    disabled: Iterable[str] = (),
    only: Iterable[str] = (),
    skip: Iterable[str] = (),
) -> dict[str, object]:
    """Run the enabled post hooks (graph enrichments) over the finished graph, returning
    {name: report}. Shared by the full-ingest tail and the incremental resync join so
    enable/disable + ordering behave identically on both. `run(writer, repo, target_sha)` is
    called per hook, each isolated in try/except -- one failing never aborts the rest.
    `disabled` (config) and `skip` (per-run) both suppress; `only` (per-run) overrides both."""
    reports: dict[str, object] = {}
    for enrichment in select_enrichments(discover_graph_enrichments(), disabled=disabled, only=only, skip=skip):
        try:
            report = enrichment.run(writer, source, target_sha)
        except Exception as e:  # one hook failing must not abort ingestion/resync
            logger.exception("Enrichment %s failed", enrichment.name)
            reports[enrichment.name] = {"error": f"{type(e).__name__}: {e}"}
            continue
        if report is not None:
            reports[enrichment.name] = report
            logger.info("Enrichment %s: %s", enrichment.name, report)
    return reports


def _ingest_from_source(
    writer: GraphWriter,
    source: Source,
    target_sha: str,
    *,
    source_uri: str,
    project_name: str,
    solution_name: str | None,
    ref: str | None,
    cache_root: Path | None,
    started: float,
    auto_install: bool | None = None,
    deep: str = "async",
    async_eligible: bool = True,
    embed: str | None = None,
    enrichment_only: tuple[str, ...] = (),
    enrichment_skip: tuple[str, ...] = (),
) -> RepoIngestReport:
    """The source-agnostic core shared by ingest_repository (git) and
    ingest_directory (local): dispatch every registered file-based language against an
    already-prepared `source`/`target_sha`, run the enrichment passes over the
    finished graph, and assemble the report. `source` is duck-typed -- it only needs
    the list_files/read_blob/working_tree surface both GitRepoCache and
    LocalDirectorySource provide.

    When `auto_install` is on (opt-in; see core/plugins.py), a file type present in the
    source that no installed plugin claims but the catalog knows triggers a PyPI install of
    the providing `auryn-<lang>` package, after which the languages are re-discovered."""
    languages = discover_git_languages()
    languages, auto_installed = _autoinstall_missing(source, target_sha, languages, auto_install)
    logger.info("Registered file-based languages: %s", ", ".join(sorted(languages)) or "<none>")

    if deep not in DEEP_MODES:
        raise ValueError(f"deep must be one of {DEEP_MODES}, got {deep!r}")

    # `.h` is statically the C ingestor's, but a `.h` with C++ content belongs to C++ (see
    # core.cpp_headers: the C grammar would mangle a class-in-header). Reassign the count so a
    # header-only C++ project (only `.h`, no `.cc`) is still detected as C++, and a pure-C project
    # doesn't spin up the C++ engine for nothing. Computed whenever the C++ ingestor is registered --
    # `auryn-cpp` can be installed without `auryn-c`, and a header-only C++ project must still be found.
    cpp_header_n = 0
    if "cpp" in languages or "c" in languages:
        # Compute the C++-content `.h` count whenever EITHER engine is present -- the C ingestor disowns
        # these `.h` in its walk regardless of whether C++ is installed, so its detection count must
        # exclude them either way (otherwise C reports ingesting headers whose classes it actually skips).
        header_excludes = languages["c"].excluded_dir_names if "c" in languages else languages["cpp"].excluded_dir_names
        cpp_header_n = len(cpp_header_paths(source, target_sha, set(header_excludes)))
    if cpp_header_n and "cpp" not in languages:
        # C++-content headers exist but the C++ engine isn't installed: the C ingestor skips them (the C
        # grammar would mangle the class), so warn rather than silently drop their classes/namespaces.
        logger.warning("%d C++-content .h header(s) found but the C++ ingestor (auryn-cpp) is not installed "
                       "-- their classes/namespaces are skipped; install auryn-cpp to ingest them", cpp_header_n)

    # Detect present languages once (a cheap suffix scan; engines are still unimported).
    present: list[tuple[GitLanguage, int]] = []
    claimed_suffixes: set[str] = set()
    for lang in languages.values():
        claimed_suffixes.update(s.lower() for s in lang.file_suffixes)
        files_detected = _detect(source, target_sha, lang)
        if lang.language == "c":
            files_detected -= cpp_header_n      # these `.h` are C++; the C ingestor skips them
        elif lang.language == "cpp":
            files_detected += cpp_header_n      # ...and the C++ ingestor claims them
        if files_detected <= 0:
            # A header that flips C<->C++ can drop its old language to zero files; removing the last
            # `Chart.yaml`/`values.yaml` drops helm to zero. Skipping would strand that language's prior
            # nodes (incremental != full). So if it has NO files now but DOES have nodes already in the
            # graph, still run its sync -- it recomputes to empty and deletes the stale subgraph. A fresh
            # or genuinely-absent language has no such nodes, so it still skips (no wasted engine import).
            scheme = {"c": "c://", "cpp": "cpp://", "helm": "helm://",
                      "kustomize": "kustomize://", "ci": "ci://"}.get(lang.language)
            has_stale = bool(scheme) and _graph_has_prefix(writer, scheme)
            if not has_stale:
                logger.info("No %s files in this source -- skipping", lang.language)
                continue
            logger.info("No %s files in this source -- running cleanup (header(s) changed language)", lang.language)
        else:
            logger.info("Detected %d %s file(s) -- ingesting", files_detected, lang.language)
        present.append((lang, files_detected))

    # PHASE 1 (fast): every present language's primary `sync`. For a two-phase language this
    # is the quick, shallow pass (Python's stdlib-ast engine); for a single-phase language
    # it's the whole extraction. This is what the graph is queryable at the moment it's done.
    fast: dict[str, tuple[LanguageIngestResult | None, str | None]] = {}
    ctx = (source_uri, project_name, solution_name, ref, cache_root)
    for lang, _ in present:
        fast[lang.language] = _run_phase(writer, source, target_sha, lang, lang.sync, "fast", *ctx)

    # PHASE 2 (deep): upgrade two-phase languages whose fast pass succeeded, per `deep`.
    to_deepen = [lang for lang, _ in present if lang.is_two_phase() and fast[lang.language][1] is None]
    deep_results, deep_status = _run_deep_phase(writer, source, target_sha, to_deepen, deep, *ctx,
                                                async_eligible=async_eligible, embed=embed)

    # A C/C++ cleanup sync (above) deletes a now-fileless language's symbols but leaves its Project node,
    # which a full recompute of the same state never creates. Sweep a c:// / cpp:// Project that has NO
    # id-descendants left (everything it owned is gone). Checked by id-prefix, NOT by out-edges -- a C
    # Project legitimately has zero outgoing edges (its files are id-descendants, not edge children), so an
    # edge test would wrongly delete every live C project. Per-project + `/`-bounded, so a real project
    # (with descendants) and sibling projects in a multi-repo graph are untouched.
    if "c" in languages or "cpp" in languages:
        # Scope to THIS project's own c:// / cpp:// ids (id == scheme+project_name), not every C/C++
        # Project in the graph -- an unrelated repo's project must not be swept while syncing this one.
        for scheme in ("c://", "cpp://"):
            pid = f"{scheme}{project_name}"
            try:
                exists = writer.ro_query("MATCH (p:Project {id: $id}) RETURN 1", {"id": pid}).result_set
            except Exception:
                exists = []  # empty graph (RO_QUERY on an unwritten key) -- nothing to sweep
            if not exists:
                continue
            # Descendant check on the shared :Entity id index (an index seek), not a label-less
            # `MATCH (n)` all-node scan (confirmed via graph_client's id-range index convention).
            has_descendant = writer.ro_query(
                "MATCH (n:Entity) WHERE n.id STARTS WITH $d RETURN 1 LIMIT 1", {"d": pid + "/"}).result_set
            if not has_descendant:
                writer.query("MATCH (p:Project {id: $id}) DETACH DELETE p", {"id": pid})

    reports: list[LanguageReport] = []
    for lang, files_detected in present:
        fast_result, fast_error = fast[lang.language]
        reports.append(
            LanguageReport(
                language=lang.language,
                files_detected=files_detected,
                result=fast_result,
                error=fast_error,
                deep_result=deep_results.get(lang.language),
                deep_status=deep_status.get(lang.language),
            )
        )

    unsupported = _scan_unsupported(source, target_sha, claimed_suffixes)
    if unsupported:
        logger.info(
            "Detected files for unsupported languages (not ingested): %s",
            ", ".join(f"{u['language']} ({u['files']})" for u in unsupported),
        )

    # Enrichment passes ("post hooks") run after all languages -- they read the finished graph
    # and synthesize derived edges (e.g. the ORM model<->table or HTTP call<->endpoint seam). The same
    # pass runs on the incremental resync join (run_post_hooks), so enable/disable + ordering
    # behave identically on both paths. Install-wide + per-repo disables come from config;
    # enrichment_only/_skip are per-run overrides passed by the CLI/MCP caller.
    enrichments = run_post_hooks(
        writer, source, target_sha,
        disabled=effective_disabled_hooks(_source_local_dir(source, source_uri)),
        only=enrichment_only,
        skip=enrichment_skip,
    )

    # Provenance is opt-in and kept in its own labels/IDs so the code delta engine never
    # owns or removes it. Run the potentially large history/API pass off the ingest path, just
    # like deep analysis: callers can query the code graph immediately while provenance fills in.
    from auryn.core.app_config import load_config
    if load_config().git_info_enabled:
        if isinstance(source, GitRepoCache):
            git_repo_path = source._path
        elif isinstance(source, WorkingTreeSource):
            git_repo_path = source._root
        else:
            git_repo_path = None
        sib = writer.sibling()
        # Detach to a subprocess only when a config-based reconnect will reach THIS backend: a
        # network FalkorDB (client has select_graph), not an ad-hoc --host (embed forced "inline")
        # or an embedded in-process DB. Otherwise run inline so it still completes.
        detachable = (embed != "inline" and os.name == "posix"
                      and hasattr(getattr(getattr(sib, "_graph", None), "client", None), "select_graph"))
        _spawn_git_info(sib, git_repo_path, source_uri, detachable=detachable)
    else:
        # The setting describes graph contents as well as future collection. This also
        # reconciles a direct config.toml edit on the next ingest.
        try:
            from auryn.core.git_info import purge_git_info
            removed = purge_git_info(writer)
            if removed:
                logger.info("Git information disabled; removed %d provenance nodes", removed)
        except Exception:
            logger.exception("Git information cleanup failed; code ingestion succeeded")

    # Opt-in semantic layer: once every language's graph is settled, build/refresh embeddings so
    # the MCP `search_code` tool works. Off by default; enable with AURYN_EMBED=1 (see
    # embeddings.embedder.embeddings_enabled). By default the heavy compute is handed to the
    # detached embed daemon ("background"), so this returns -- and the graph is queryable -- the
    # moment it's built, rather than blocking on every vector.
    _maybe_embed(writer, deep, embed)

    elapsed = time.monotonic() - started
    ingested = sum(1 for r in reports if r.result is not None)
    logger.info("Ingestion of %r complete in %.2fs (%d language(s) ingested)", project_name, elapsed, ingested)
    return RepoIngestReport(
        repo_uri=source_uri,
        project_name=project_name,
        solution_name=solution_name,
        commit_sha=target_sha,
        ref=ref,
        languages=reports,
        unsupported_detected=unsupported,
        elapsed_seconds=elapsed,
        enrichments=enrichments,
        auto_installed=auto_installed,
    )


def _maybe_embed(writer: GraphWriter, deep: str, embed: str | None = None) -> None:
    """Build/refresh embeddings after ingest when opted in (AURYN_EMBED=1), the way `embed`/config
    says to (see embeddings.embedder.embed_mode):

    - **background** (default): enqueue the graph for the detached embed daemon and return at once,
      so the ingest call -- and the already-built, queryable graph -- isn't held up by the heavy
      vector compute. The daemon reconnects to the configured backend on its own.
    - **inline**: embed synchronously here. Used by callers whose connection a separate process
      couldn't reach (serverless/quickstart, explicit --host), and available as an override.
    - **off**: skip.

    Contained: any failure (e.g. the model can't be fetched offline) is logged and never fails the
    ingest, which has already succeeded."""
    from auryn.core.embeddings import embeddings_enabled
    from auryn.core.embeddings.embedder import embed_mode

    if not embeddings_enabled():
        return
    mode = embed_mode(embed)
    if mode == "off":
        return

    graph_name = getattr(getattr(writer, "_graph", None), "_name", None)
    from auryn.core.embeddings import configured_model, get_embedder
    from auryn.core.embeddings.daemon import daemon_supported

    # Background needs (a) a graph name the daemon can reconnect to and (b) a platform that can run
    # the daemon (POSIX fcntl). Without either, fall back to inline rather than queueing a request
    # nothing will ever drain -- native Windows has no daemon, so a background request would sit
    # forever and semantic search would never come up (cubic review).
    if mode == "background" and graph_name and daemon_supported():
        _embed_in_background(graph_name, deep, configured_model())
        return

    # Inline. A background deep pass is still mutating nodes, so embedding now would race it (and
    # leave stale vectors the worker won't re-scan); defer to a manual `auryn embed` in that case.
    if deep == "background":
        logger.info("Embedding deferred: deep pass still running (run `auryn embed` once it settles)")
        return
    try:
        from auryn.core.embeddings.worker import embed_graph

        stats = embed_graph(writer, get_embedder(configured_model()))
        logger.info("Embeddings: %d embedded, %d scanned (%s)", stats.embedded, stats.scanned, stats.model_id)
    except Exception:
        logger.exception("Embedding pass failed (ingest itself succeeded; run `auryn embed` to retry)")


def _embed_in_background(graph_name: str, deep: str, model: str | None = None) -> None:
    """Hand `graph_name` to the detached embed daemon. When a background deep pass is still in
    flight, wait for it first (on a daemon thread, so the caller still returns now) so the daemon
    embeds settled nodes rather than racing deep's updates -- which also means an auto-embed finally
    happens for deep=background ingests (previously they embedded nothing until a manual run)."""
    from auryn.core.embeddings.daemon import request_embed

    try:
        if deep == "background":
            def _after_deep() -> None:
                try:
                    join_deep_phase()
                    request_embed(graph_name, model)
                except Exception:
                    logger.exception("Background embed (after deep) failed to enqueue for %r", graph_name)

            threading.Thread(target=_after_deep, name="embed-after-deep", daemon=True).start()
        else:
            request_embed(graph_name, model)
        logger.info("Embedding %r in the background (graph is already queryable)", graph_name)
    except Exception:
        logger.exception("Failed to enqueue background embed for %r (run `auryn embed` to build it)", graph_name)


def _run_phase(
    writer: GraphWriter,
    source: Source,
    target_sha: str,
    lang: GitLanguage,
    sync_callable,
    phase: str,
    source_uri: str,
    project_name: str,
    solution_name: str | None,
    ref: str | None,
    cache_root: Path | None,
) -> tuple[LanguageIngestResult | None, str | None]:
    """Run one ingest phase (`lang.sync` or `lang.deep_sync`) for a language and return
    (result, error). One phase raising is contained -- logged and reported -- so it never
    aborts the other languages or the other phase."""
    try:
        result = sync_callable(
            writer,
            source,
            target_sha,
            repo_uri=source_uri,
            project_name=project_name,
            solution_name=solution_name,
            ref=ref,
            cache_root=cache_root,
        )
    except Exception as e:  # one language/phase failing must not abort the others
        logger.exception("%s-phase ingestion of %s failed", phase, lang.language)
        return None, f"{type(e).__name__}: {e}"

    logger.info(
        "Finished %s-phase %s: nodes +%d/-%d/~%d, edges +%d/-%d/~%d (%d parsed, %d cached) in %.2fs",
        phase,
        lang.language,
        result.nodes_added,
        result.nodes_removed,
        result.nodes_updated,
        result.edges_added,
        result.edges_removed,
        result.edges_updated,
        result.files_parsed,
        result.files_cached,
        result.elapsed_seconds,
    )
    return result, None


def _daemon_can_reach(writer: GraphWriter) -> bool:
    """Whether a detached deepen daemon could reach this writer's graph. The daemon reconnects to the
    CONFIGURED backend, so it can only reach a graph on a real server -- never an in-process embedded
    (falkordblite) DB (serverless quickstart, tests). The embedded client's `g.client` is raw Redis
    with no `select_graph`, while a server client's has it (see GraphWriter.sibling) -- that tells them
    apart without threading a config flag down. So an embedded ingest deepens inline instead."""
    client = getattr(getattr(writer, "_graph", None), "client", None)
    return client is not None and hasattr(client, "select_graph")


def _run_deep_phase(
    writer: GraphWriter,
    source: Source,
    target_sha: str,
    to_deepen: list[GitLanguage],
    deep: str,
    source_uri: str,
    project_name: str,
    solution_name: str | None,
    ref: str | None,
    cache_root: Path | None,
    async_eligible: bool = True,
    embed: str | None = None,
) -> tuple[dict[str, LanguageIngestResult], dict[str, str]]:
    """Second, deeper pass over the two-phase languages whose fast pass succeeded. Returns
    (deep_results, deep_status) keyed by language. See DEEP_MODES for the `deep` values.

    `async_eligible` is False when the daemon's replay (`ingest_directory(local_dir, deep="inline")`)
    couldn't faithfully reproduce THIS ingest -- a git ref-pinned source (replay would index the
    working tree's HEAD), non-default source filters, or a solution_name (both change the file set /
    node ids). Then async downgrades to inline rather than deepen a different graph."""
    deep_results: dict[str, LanguageIngestResult] = {}
    deep_status: dict[str, str] = {}
    if not to_deepen:
        return deep_results, deep_status

    names = ", ".join(lang.language for lang in to_deepen)
    if deep == "off":
        logger.info("Deep phase skipped (deep=off); graph stays shallow for: %s", names)
        return deep_results, {lang.language: "skipped" for lang in to_deepen}

    if deep == "background":
        # Upgrade in a daemon thread on a sibling writer (own Graph object, shared pooled
        # client) so the caller gets the fast graph back now. See ingest.DEEP_MODES.
        deep_writer = writer.sibling()
        for lang in to_deepen:
            _spawn_deep(deep_writer, source, target_sha, lang,
                        source_uri, project_name, solution_name, ref, cache_root)
            deep_status[lang.language] = "background"
        logger.info("Deep phase upgrading in the background for: %s", names)
        return deep_results, deep_status

    if deep == "async":
        # Hand the deep pass to the detached deepen daemon so it SURVIVES this (possibly one-shot CLI)
        # process exiting -- unlike "background"'s in-process thread. The daemon re-runs the ingest with
        # deep="inline" (the fast phase short-circuits), upgrading the graph in place. Falls back to
        # inline when there's no re-ingestable local dir (a remote-URI ingest with no checkout), no
        # graph name, or the platform can't host the daemon (needs the configured backend + fcntl).
        from auryn.core.deepen.daemon import daemon_supported, request_deepen

        local_dir = _source_local_dir(source, source_uri)
        graph_name = getattr(getattr(writer, "_graph", None), "_name", None)
        if async_eligible and local_dir and graph_name and daemon_supported() and _daemon_can_reach(writer):
            request_deepen(graph_name, local_dir, project_name, embed)
            logger.info("Deep phase queued to the background deepen daemon for: %s", names)
            return deep_results, {lang.language: "async" for lang in to_deepen}
        logger.info("Deep async unavailable (ref-pinned/filtered/solution source, no local dir / graph / "
                    "daemon, or embedded backend); running inline for: %s", names)
        # fall through to the inline path below

    # "inline" (default): run each deep upgrade now, synchronously.
    for lang in to_deepen:
        result, error = _run_phase(writer, source, target_sha, lang, lang.deep_sync, "deep",
                                   source_uri, project_name, solution_name, ref, cache_root)
        if error is None and result is not None:
            deep_results[lang.language] = result
            deep_status[lang.language] = "inline"
        else:
            deep_status[lang.language] = "error"
    return deep_results, deep_status


def _spawn_deep(writer, source, target_sha, lang, *ctx) -> None:
    """Start (and track) a daemon thread running a language's deep upgrade in the background.
    Exceptions inside are contained by _run_phase (logged, not raised), and its (result, error)
    outcome is stored ON the thread (`.deep_outcome`) so a caller can see which of ITS background
    passes FAILED via deep_failures() -- otherwise a `--deep background` timeout would leave the
    graph shallow while the command still exited 0, the exact silent failure #857 is about. Keeping
    the outcome on the thread (not a shared list) means a concurrent ingest's join, or the
    embed-after-deep waiter, can't consume or mis-attribute it."""
    outcome: dict[str, str | None] = {"lang": lang.language, "error": None}

    def _target() -> None:
        _, error = _run_phase(writer, source, target_sha, lang, lang.deep_sync, "deep(background)", *ctx)
        outcome["error"] = error

    thread = threading.Thread(target=_target, name=f"deep-{lang.language}", daemon=True)
    thread.deep_outcome = outcome  # type: ignore[attr-defined]
    collector = getattr(_tls, "collector", None)  # an active deep_failure_scope() on this thread (local)
    if collector is not None:
        collector.append(thread)
    # Publish + start under the lock so join_deep_phase()'s snapshot never catches this thread
    # registered-but-unstarted (which it could prune as not-alive, or fail to join). Clean up on error.
    with _deep_lock:
        _deep_threads.append(thread)
        try:
            thread.start()
        except BaseException:
            _deep_threads.remove(thread)
            if collector is not None:
                collector.remove(thread)
            raise


def _spawn_git_info(writer: GraphWriter, repo_path: str | None, source_uri: str,
                    *, detachable: bool = False, force_remote: bool = False) -> None:
    """Collect repository provenance OFF the code-ingest critical path.

    On a network-reachable configured backend (`detachable`), spawn a DETACHED `auryn git-info sync`
    subprocess that OUTLIVES this process -- so a one-shot `auryn ingest` doesn't kill the collection
    mid-flight (a plain daemon thread dies on interpreter exit). The worker is single-flight per graph,
    so overlapping triggers never collect twice. Otherwise -- an embedded in-process DB, an ad-hoc
    `--host`, or a platform without detach -- run it inline (blocking) so it still completes."""
    if repo_path is None:
        try:
            from auryn.core.git_info import remove_git_info_for_source
            remove_git_info_for_source(writer, source_uri)
            logger.info("Git information skipped: source is not a Git repository")
        except Exception:
            logger.exception("Git information unlink failed; code ingestion succeeded")
        return

    graph_name = getattr(getattr(writer, "_graph", None), "_name", None)
    if detachable and graph_name:
        try:
            from auryn.core.home import run_dir
            run_dir().mkdir(parents=True, exist_ok=True)
            argv = [sys.executable, "-m", "auryn", "git-info", "sync", "--graph", graph_name,
                    "--force-remote" if force_remote else "--no-force-remote",
                    "--source-uri", source_uri, str(repo_path)]
            # `with` closes the PARENT's fd right after Popen returns (the child already dup'd it) --
            # avoids leaking a descriptor on every detached ingest. The whole spawn is guarded so an
            # unwritable run dir logs and returns instead of failing the already-written code graph.
            with open(run_dir() / "git-info.log", "ab") as log:
                subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                 start_new_session=True, close_fds=True)
            logger.info("Git information sync spawned (detached) for graph %r", graph_name)
        except Exception:
            logger.exception("Git information spawn failed; code ingestion succeeded")
        return

    try:  # inline (blocking) -- the only way an embedded / ad-hoc-host backend completes reliably
        from auryn.core.git_info import sync_git_info
        summary = sync_git_info(writer, repo_path, source_uri=source_uri, force_remote=force_remote)
        logger.info("Git information synced (inline): %s", summary)
    except Exception:
        logger.exception("Git information sync failed; code ingestion succeeded")


def deep_threads() -> list[threading.Thread]:
    """The background deep passes this process has started and not yet joined -- so a caller can
    tell the ones its own ingest started from another repo's (see mcp/server.py)."""
    with _deep_lock:
        return list(_deep_threads)


def join_deep_phase(timeout: float | None = None) -> None:
    """Block until the background deep passes started by this process finish. Optional for a
    long-lived server (the daemon threads upgrade the graph on their own); used by tests and
    by any caller that must not exit before the deep graph is ready. Pairs with deep_failures() to
    tell which (if any) failed -- this only waits (and is safe to call from several places, e.g. the
    embed-after-deep waiter, without consuming that signal)."""
    with _deep_lock:  # snapshot under the lock (consistent with _spawn_deep's publish+start)
        snapshot = list(_deep_threads)
    for thread in snapshot:
        thread.join(timeout)  # join OUTSIDE the lock so a spawn isn't blocked for the whole wait
    with _deep_lock:
        _deep_threads[:] = [t for t in _deep_threads if t.is_alive()]


def deep_failures(threads: list[threading.Thread]) -> list[str]:
    """The languages whose background deep pass FAILED, among `threads`. Reads the outcome each
    thread carries; call AFTER joining so every outcome is final. Non-consuming -- the caller owns
    the list, so repeated or concurrent readers don't race (#857)."""
    failed = [
        t.deep_outcome["lang"]  # type: ignore[attr-defined]
        for t in threads
        if getattr(t, "deep_outcome", None) and t.deep_outcome.get("error") is not None  # type: ignore[attr-defined]
    ]
    return sorted(set(failed))


@contextlib.contextmanager
def deep_failure_scope():
    """Surface `--deep background` failures of the ingest run INSIDE this scope. While active, every
    deep thread spawned on this thread is collected (see _tls); on exit the scope joins exactly those
    threads (holding any caller lock for them, as a bare join_deep_phase() would) and fills the
    yielded holder's "failed" with their failed languages. Thread-local + caller-owned, so it is
    race-free across concurrent ingests and the embed-after-deep waiter can't consume the signal --
    the robust replacement for diffing the shared thread list (#857).

        with deep_failure_scope() as bg:
            report = ingest_directory(...)
        _echo_ingest_report(report, background_failures=bg["failed"])
    """
    collected: list[threading.Thread] = []
    prev = getattr(_tls, "collector", None)
    _tls.collector = collected
    holder: dict[str, set[str]] = {"failed": set()}
    try:
        yield holder
    finally:
        _tls.collector = prev
        for thread in collected:
            thread.join()
        holder["failed"] = set(deep_failures(collected))


def _graph_has_prefix(writer: GraphWriter, id_prefix: str) -> bool:
    """Whether the graph holds any node whose id starts with `id_prefix`. Used to decide if a now-fileless
    C/C++ language still has stale nodes to clean up after a header flipped language. Returns False on a
    graph nothing has been written to yet (FalkorDB RO_QUERY raises against an empty key) -- a fresh
    ingest has nothing stale."""
    try:
        return bool(writer.ro_query(
            "MATCH (n) WHERE n.id STARTS WITH $p RETURN 1 LIMIT 1", {"p": id_prefix}).result_set)
    except Exception:
        return False


def _detect(source: Source, target_sha: str, lang: GitLanguage) -> int:
    """How many of this language's files are present at `target_sha`, by basename glob
    (`match_patterns` unions the suffixes and `file_globs`). One glob pass, so a file
    matching several patterns is counted once (globs, unlike disjoint suffixes, can overlap)."""
    excluded = set(lang.excluded_dir_names)
    return len(source.list_files_glob(target_sha, lang.match_patterns(), excluded))


def _autoinstall_missing(
    source: Source, target_sha: str, languages: dict[str, GitLanguage], auto_install: bool | None
) -> tuple[dict[str, GitLanguage], list[str]]:
    """When opted in, install catalog plugins for file types present in `source` that no
    installed language claims, then re-discover. Returns the (possibly larger) language map;
    a no-op (returns `languages` unchanged) when auto-install is off or nothing is missing."""
    from auryn.core import plugins

    if not plugins.auto_install_enabled(auto_install):
        return languages, []
    claimed = {suffix for lang in languages.values() for suffix in lang.file_suffixes}
    present_unclaimed = {
        suffix
        for suffix in plugins.PLUGIN_PACKAGES
        if suffix not in claimed and source.list_files(target_sha, suffix, _UNSUPPORTED_SCAN_EXCLUDES)
    }
    pkgs = plugins.packages_for_suffixes(present_unclaimed)
    if not pkgs:
        return languages, []
    logger.info("Auto-install: source has unclaimed %s; installing %s", sorted(present_unclaimed), sorted(pkgs))
    installed = [pkg for pkg in sorted(pkgs) if plugins.install_plugin(pkg)]
    return (discover_git_languages() if installed else languages), installed


def _scan_unsupported(source: Source, target_sha: str, claimed_suffixes: set[str]) -> list[dict]:
    """Group counts of files whose extension maps to a known-but-unregistered
    language, skipping any extension a registered language already handles."""
    by_language: dict[str, dict] = {}
    for ext, language in _KNOWN_UNSUPPORTED.items():
        if ext in claimed_suffixes:
            continue
        count = len(source.list_files(target_sha, ext, _UNSUPPORTED_SCAN_EXCLUDES))
        if count == 0:
            continue
        entry = by_language.setdefault(language, {"language": language, "extensions": [], "files": 0})
        entry["extensions"].append(ext)
        entry["files"] += count
    return sorted(by_language.values(), key=lambda e: e["files"], reverse=True)


def _derive_project_name(repo_uri: str) -> str:
    """The repo's own name -- its last path segment, stripped of a trailing slash
    and a `.git` suffix. Works for local paths, https URLs, and scp-style
    `git@host:org/repo.git` alike (the last `/`-delimited segment is the repo name
    in every case)."""
    tail = repo_uri.rstrip("/").replace("\\", "/").rsplit("/", 1)[-1]
    tail = tail.removesuffix(".git")
    return tail or "repo"
