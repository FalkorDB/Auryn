"""Ingestor registration and auto-discovery.

Two parallel registries live here, both populated the same way -- by importing
every subpackage under `auryn.ingestors` so its registration side effects run:

- `register_ingestor` / `discover_ingestors`: the BaseIngestor subclasses reachable
  as `auryn run <name>` (a source read against a live source -- a DB connection
  or a local directory).
- `register_git_language` / `discover_git_languages`: file-based ingestors that can
  process a git repo's blobs, keyed by the file suffixes they own. This is what the
  multi-language ingestion endpoint (core/ingest.py) dispatches over -- adding a
  language there means registering a GitLanguage, never editing the endpoint.

Adding a new ingestor never requires editing this file: create a new subpackage
under `auryn.ingestors`, subclass BaseIngestor and/or register a GitLanguage, and
discovery finds it automatically.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import Any

from auryn.core.base_ingestor import BaseIngestor

_REGISTRY: dict[str, type[BaseIngestor]] = {}
_GIT_REGISTRY: dict[str, "GitLanguage"] = {}
_ENRICHMENT_REGISTRY: dict[str, "GraphEnrichment"] = {}


def register_ingestor(name: str):
    def decorator(cls: type[BaseIngestor]) -> type[BaseIngestor]:
        cls.name = name
        _REGISTRY[name] = cls
        return cls

    return decorator


@dataclass(frozen=True)
class LanguageIngestResult:
    """The uniform delta one language's `GitLanguage.sync` reports back, so the
    ingestion endpoint's report doesn't special-case each language. Each ingestor's
    own native stats (e.g. csharp's SyncStats) map onto this by a trivial field
    copy in its adapter."""

    nodes_added: int = 0
    nodes_updated: int = 0
    nodes_removed: int = 0
    edges_added: int = 0
    edges_updated: int = 0
    edges_removed: int = 0
    files_parsed: int = 0
    files_cached: int = 0
    commit_sha: str = ""
    elapsed_seconds: float = 0.0
    broken_files: list[str] = field(default_factory=list)  # changed files that failed to parse (last-good kept)


@dataclass(frozen=True)
class GitLanguage:
    """A language that can be ingested straight from a git repository's files.

    `file_suffixes` are the path suffixes (lowercased, dot-included, e.g. ".cs")
    that identify this language's source; the endpoint uses them both to decide the
    language is present in a repo and to hand the right blobs to `sync`.
    `sync` moves this language's subgraph to `target_sha` and returns the delta --
    signature:
        sync(writer, repo, target_sha, *, repo_uri, project_name, solution_name,
             ref, cache_root) -> LanguageIngestResult
    where `repo` is an already-fetched GitRepoCache and `target_sha` an
    already-resolved commit (the orchestrator fetches/resolves once and shares it
    across every language), and `ref` is the originally requested branch/tag/sha
    (or None) recorded for provenance. See core/ingest.py for how this is
    called; ingestors/csharp/git_sync.py for the reference registration.
    """

    language: str
    file_suffixes: tuple[str, ...]
    excluded_dir_names: frozenset[str]
    sync: Callable[..., Any]
    deep_sync: Callable[..., Any] | None = None
    """Optional second, deeper pass for two-phase (dynamic) languages. When set, the
    ingestion endpoint runs `sync` first (fast, e.g. Python's stdlib-`ast` engine) so the
    graph is queryable quickly, then upgrades it in place with `deep_sync` (e.g. Jedi's
    inferred types + resolved calls) -- inline or in the background. Same signature and
    return type as `sync`; because both engines emit identical node ids, the deep pass is a
    pure delta over the fast one. None for static languages that have only one pass."""
    file_globs: tuple[str, ...] = ()
    """Basename GLOB patterns (fnmatch, case-sensitive) that identify this language's files, for the
    cases a bare `endswith` suffix can't express: a prefix/infix variant (`Dockerfile.*`,
    `compose.*.yml`) or an exact-basename anchor that fires only when present (`Chart.yaml`, so Helm
    isn't woken by every `.yaml`). ADDITIVE to `file_suffixes` (each suffix `s` is equivalent to the
    glob `*s`); detection and the save-watcher match on the UNION (see `match_patterns`). Overlapping
    patterns across languages (Helm `Chart.yaml` vs k8s `*.yaml`) are NOT exclusive ownership -- each
    producer's `sync` still content-sniffs to decide what it actually owns (ADR 0007 §7). Declared LAST
    (after `deep_sync`) so it never shifts a positional `GitLanguage(...)` registration."""

    def is_two_phase(self) -> bool:
        return self.deep_sync is not None

    def match_patterns(self) -> tuple[str, ...]:
        """The basename globs that identify this language's files: each `file_suffixes` entry `s` as the
        glob `*s` (so `.cs` -> `*.cs`, `Dockerfile` -> `*Dockerfile`, preserving the old `endswith`
        semantics), unioned with `file_globs`, de-duplicated. The single source of truth for both
        detection and the save-watcher."""
        return match_patterns(self.file_suffixes, self.file_globs)


def match_patterns(file_suffixes: tuple[str, ...], file_globs: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Basename globs for a `(file_suffixes, file_globs)` pair: each suffix `s` becomes `*s` (preserving
    the old `endswith` semantics), unioned with `file_globs`, order-preserving de-duplicated. The one
    place the union is computed -- `GitLanguage.match_patterns` and every plugin's git_sync call it, so
    detection and ingestion can never match different files."""
    return tuple(dict.fromkeys((*(f"*{s}" for s in file_suffixes), *file_globs)))


def register_git_language(language: GitLanguage) -> None:
    _GIT_REGISTRY[language.language] = language


def _import_ingestor_subpackages() -> None:
    """Import every subpackage under auryn.ingestors so its registration side
    effects (@register_ingestor decorators, register_git_language() calls) run.
    Shared by both discovery functions so neither has to know which registry a
    given import will populate.

    Tolerates the namespace being absent: a lean core-only install (e.g. `uv tool install
    ingestor` with no language plugins, and no bare source tree either) has no
    `auryn.ingestors` at all. That's not an error -- there are simply no bundled ingestors
    to load; language plugins are discovered via entry points (or auto-installed on demand)."""
    try:
        import auryn.ingestors as ingestors_pkg
    except ModuleNotFoundError:
        return

    for module_info in pkgutil.iter_modules(ingestors_pkg.__path__):
        importlib.import_module(f"{ingestors_pkg.__name__}.{module_info.name}")


# Plugins declare themselves in one of three entry-point groups, split by concern so a
# consumer loads only what it needs -- and, crucially, no more. The multi-language
# ingestion endpoint asks only for `languages` + `enrichments`; it never imports the
# live-DB `ingestors` group (csharp's Roslyn/tree-sitter, PL/SQL's ANTLR, SQL's
# SQLAlchemy), which the `auryn run <name>` CLI path loads instead. And a language's
# heavy engine stays unimported until a repo actually contains its files, because the
# GitLanguage a plugin registers carries a *lazy* `sync` (see each package's plugin.py).
_LANGUAGE_GROUP = "auryn.languages"       # register_git_language (file suffix-scan)
_ENRICHMENT_GROUP = "auryn.enrichments"   # register_graph_enrichment (post-passes)
_AURYN_GROUP = "auryn.ingestors"       # register_ingestor (live-source BaseIngestors)
_ALL_GROUPS = (_LANGUAGE_GROUP, _ENRICHMENT_GROUP, _AURYN_GROUP)

_bare_tree_loaded = False


def _load_group(group: str) -> None:
    """Import every plugin registered in one entry-point `group` so its registration side
    effects run -- and only that group, so discovering (say) file-based languages never
    drags in the live-DB ingestors' heavy imports.

    Falls back to importing the bundled `auryn.ingestors.*` subpackages directly only
    when *no* group has any entry points -- a bare source tree not reinstalled since the
    entry points were declared. When other groups are populated, an empty group is taken at
    face value (a distribution may legitimately ship only enrichments, say)."""
    from importlib.metadata import entry_points

    eps = list(entry_points(group=group))
    if eps:
        for ep in eps:
            ep.load()  # importing the target module runs its registration side effect
        return

    global _bare_tree_loaded
    if _bare_tree_loaded or any(entry_points(group=g) for g in _ALL_GROUPS):
        return
    _import_ingestor_subpackages()
    _bare_tree_loaded = True


def discover_ingestors() -> dict[str, type[BaseIngestor]]:
    """Load the live-source BaseIngestors (the `auryn run <name>` targets) so their
    @register_ingestor decorators run, then return the accumulated registry."""
    _load_group(_AURYN_GROUP)
    return dict(_REGISTRY)


def discover_git_languages() -> dict[str, GitLanguage]:
    """Load every file-based language plugin so their register_git_language() calls run,
    then return the accumulated registry. Import-light: each plugin registers a GitLanguage
    with a lazy `sync`, so no language's extraction engine is imported here -- only when its
    files are actually found in a source."""
    _load_group(_LANGUAGE_GROUP)
    return dict(_GIT_REGISTRY)


@dataclass(frozen=True)
class GraphEnrichment:
    """A post-processing pass ("hook") that runs *after* the language ingestors and enriches
    the finished graph -- reading what's already there and synthesizing derived nodes/edges
    (e.g. the ORM model<->table or HTTP call<->endpoint seam). Registered like the other
    ingestors and run by the ingestion endpoint on both the full and incremental paths,
    never a bespoke CLI command.

    `run` signature: run(writer, repo, target_sha) -> dict | None
    where `writer` is a GraphWriter over the target graph (use its query/ro_query for
    reads and merge_*/query for writes), `repo` is the already-fetched GitRepoCache,
    and `target_sha` the resolved commit. Returns a small report dict (or None to
    signal it found nothing to do, e.g. its input file isn't in the repo). Must be
    idempotent -- it recomputes its own layer each run (so re-running after an
    incremental resync is correct against the post-delta graph).

    `phase` is reserved: only "post" hooks are executed today ("pre" may be discovered but is
    never run). `order` sorts hooks ascending within a phase. `source` records where the hook
    came from -- "plugin" (an ``auryn.enrichments`` entry point) or "local" (a file dropped
    in ``~/.auryn/hooks/``); it's display-only (``auryn hooks list``)."""

    name: str
    run: Callable[..., Any]
    phase: str = "post"       # reserved; only "post" is executed today
    order: int = 100          # ascending run order within a phase
    source: str = "plugin"    # "plugin" (entry point) | "local" (~/.auryn/hooks)


def register_graph_enrichment(enrichment: GraphEnrichment) -> None:
    _ENRICHMENT_REGISTRY[enrichment.name] = enrichment


# Import failures for local hook files, keyed by file stem -> "ExcType: msg". Surfaced by
# `auryn hooks list` so a broken drop-in is visible rather than silently missing.
_LOCAL_HOOK_ERRORS: dict[str, str] = {}
_local_hooks_loaded = False


def _load_local_hooks() -> None:
    """Import every ``~/.auryn/hooks/*.py`` so user hooks register. Zero-ceremony: a file
    that defines a top-level ``run`` is auto-registered (name = filename stem, unless it sets a
    module-level ``NAME``; ``ORDER`` overrides the default order). A file may instead call
    ``register_graph_enrichment`` itself -- both are honoured and tagged ``source="local"``.

    Best-effort and isolating: each file is imported under its own try/except, so one broken
    hook is recorded in ``_LOCAL_HOOK_ERRORS`` (not raised) and the rest still load. A local
    file whose name collides with an already-registered plugin hook is skipped with a warning --
    a drop-in can't silently shadow a shipped hook. Machine-wide only; never reads repo files."""
    global _local_hooks_loaded
    if _local_hooks_loaded:
        return
    _local_hooks_loaded = True

    import importlib.util
    import logging

    from auryn.core.home import hooks_dir

    log = logging.getLogger("auryn.registry")
    d = hooks_dir()
    if not d.is_dir():
        return

    for path in sorted(d.glob("*.py")):
        if path.name.startswith("_"):
            continue
        stem = path.stem
        _LOCAL_HOOK_ERRORS.pop(stem, None)
        before = dict(_ENRICHMENT_REGISTRY)  # snapshot: detect new AND clobbered names
        try:
            spec = importlib.util.spec_from_file_location(f"auryn._local_hooks.{stem}", path)
            if spec is None or spec.loader is None:
                raise ImportError(f"could not load spec for {path}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            # Did the file register anything itself (new key, or an object that replaced an
            # existing one)? A colliding name is restored to the prior owner -- a local drop-in
            # can never shadow a plugin (or an earlier-loaded local) hook.
            touched = {n for n, h in _ENRICHMENT_REGISTRY.items() if n not in before or h is not before[n]}
            if touched:
                for name in touched:
                    if name in before:
                        _ENRICHMENT_REGISTRY[name] = before[name]
                        log.warning("local hook %s tried to redefine %r -- keeping the existing one", path, name)
                    else:
                        _ENRICHMENT_REGISTRY[name] = replace(_ENRICHMENT_REGISTRY[name], source="local")
                continue

            run = getattr(module, "run", None)
            if not callable(run):
                raise AttributeError(
                    "a hook file must define a top-level run(writer, repo, target_sha) "
                    "or call register_graph_enrichment()"
                )
            name = getattr(module, "NAME", stem)
            if name in before:
                log.warning("local hook %s shadows a registered hook %r -- skipping the local file", path, name)
                continue
            register_graph_enrichment(
                GraphEnrichment(name=name, run=run, order=int(getattr(module, "ORDER", 100)), source="local")
            )
        except Exception as e:  # a broken drop-in is recorded, never fatal to discovery
            _LOCAL_HOOK_ERRORS[stem] = f"{type(e).__name__}: {e}"
            log.warning("skipping broken hook %s: %s", path, e)


def discover_graph_enrichments() -> dict[str, GraphEnrichment]:
    """Load hooks from both sources so their registrations run, then return the accumulated
    registry: entry-point plugins (the ``auryn.enrichments`` group) plus user files under
    ``~/.auryn/hooks/``. Plugins load first, so a local file can't shadow one."""
    _load_group(_ENRICHMENT_GROUP)
    _load_local_hooks()
    return dict(_ENRICHMENT_REGISTRY)


def local_hook_errors() -> dict[str, str]:
    """Import errors from the last local-hook discovery (stem -> message); for `hooks list`."""
    return dict(_LOCAL_HOOK_ERRORS)


def select_enrichments(
    all_hooks: dict[str, GraphEnrichment],
    *,
    disabled: Iterable[str] = (),
    only: Iterable[str] = (),
    skip: Iterable[str] = (),
) -> list[GraphEnrichment]:
    """The post-phase hooks to run, in (order, name) order.

    Precedence: an explicit per-run ``only`` wins outright (run exactly those, ignoring the
    disabled set -- an explicit request overrides a stored preference); otherwise run everything
    except ``disabled ∪ skip``. "pre" hooks are always excluded (reserved, never executed today).
    Unknown names in only/skip/disabled are ignored here -- the CLI validates and errors on them."""
    only_set = set(only)
    drop = set(disabled) | set(skip)
    chosen = [
        h for h in all_hooks.values()
        if h.phase == "post" and (h.name in only_set if only_set else h.name not in drop)
    ]
    return sorted(chosen, key=lambda h: (h.order, h.name))
