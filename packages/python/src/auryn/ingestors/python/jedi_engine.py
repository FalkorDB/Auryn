"""Jedi-backed semantic oracle -- the default deep engine for Python.

Jedi is a pure-Python, pip-installable static-analysis library (the engine behind many
IDEs). It runs in-process, so unlike an out-of-process type checker it needs no second
runtime, no build step, and no vendored toolchain -- `uv sync` gets it. This oracle wraps
a Jedi `Project` + per-file `Script`s and answers the two queries resolution.py needs:
inferred types at a position, and the resolved dotted target of a call.

Jedi reads source from disk, so the caller materializes a working tree first (git_sync's
_jedi_sync_at) and hands this oracle that directory; module paths are repo-relative under it.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import jedi

from auryn.core.parallel import make_process_pool
from auryn.ingestors.python.raw_model import RawFunction, RawModule

logger = logging.getLogger(__name__)


def _dotted_by_file(source_dir: str, modules: list[RawModule]) -> dict[str, str]:
    """Absolute file path (the form Jedi reports as a definition's `module_path`) -> the
    dotted name the ast pass gave that file."""
    root = Path(source_dir).absolute()
    return {str(root / m.path): m.dotted for m in modules}


def _callee_name(full_name: str, module_name: str, module_path: str, dotted_by_file: dict[str, str]) -> str:
    """Jedi's dotted name for a callee, re-anchored at the name the ast pass gave the file
    defining it. Jedi names a module by its path under the *project* root (`src/pkg/db.py`
    -> 'src.pkg.db'); ids.dotted_module_path anchors it at its *import* root ('pkg.db'), and
    that is the name resolution's index is keyed by -- left as-is, a same-module or
    relative-import callee in a `src/` layout misses the index and degrades to an
    `extern://` placeholder. A callee defined outside the project keeps Jedi's name, and so
    does one Jedi names as an importer spelled it rather than under its module (it stopped at
    an import inside a project file) -- there is no module prefix to swap."""
    dotted = dotted_by_file.get(module_path)
    if dotted is None or not (full_name + ".").startswith(module_name + "."):
        return full_name
    return dotted + full_name[len(module_name):]


class JediOracle:
    """Implements resolution.SemanticOracle over a materialized source directory."""

    def __init__(self, source_dir: str, modules: list[RawModule] = ()):
        self._root = Path(source_dir)
        self._project = jedi.Project(path=str(self._root))
        self._scripts: dict[str, jedi.Script] = {}
        self._dotted_by_file = _dotted_by_file(source_dir, modules)

    def _script(self, module_path: str) -> jedi.Script:
        script = self._scripts.get(module_path)
        if script is None:
            script = jedi.Script(path=str(self._root / module_path), project=self._project)
            self._scripts[module_path] = script
        return script

    def infer_type(self, module_path: str, line: int, col: int) -> str | None:
        """The inferred type name at (line, col), e.g. 'str' / 'list' / 'Widget'. Jedi
        returns nothing for an un-inferrable name (unannotated param with no usable info),
        which becomes an honest `unresolved` in the graph."""
        if not line:
            return None
        try:
            for d in self._script(module_path).infer(line, col):
                if d.name:
                    return d.name
        except Exception as e:  # jedi can raise on odd positions; degrade to unresolved
            logger.debug("jedi.infer failed at %s:%d:%d (%s)", module_path, line, col, e)
        return None

    def resolve_call(self, module_path: str, line: int, col: int) -> str | None:
        """The callee's dotted full name at (line, col) via goto, following imports --
        reaches receiver-typed calls (`conn.execute` -> 'app.db.Connection.execute') as
        well as bare names. Returns None when Jedi can't bind it (dynamic dispatch)."""
        if not line:
            return None
        try:
            for d in self._script(module_path).goto(line, col, follow_imports=True):
                if d.full_name:
                    return _callee_name(d.full_name, d.module_name, str(d.module_path), self._dotted_by_file)
        except Exception as e:
            logger.debug("jedi.goto failed at %s:%d:%d (%s)", module_path, line, col, e)
        return None


# -- parallel precompute -----------------------------------------------------------
#
# Jedi inference at every position is the ingestor's dominant cost (~130x the ast engine).
# It parallelizes cleanly: each file's queries are independent, so we fan the per-file Jedi
# work out across a process pool, then hand resolution.py a pure dict-lookup oracle. This is
# the same shape as the C# ingestor's ProcessPoolExecutor per-file parsing.


class PrecomputedOracle:
    """resolution.SemanticOracle backed by maps filled up front (in parallel), so
    resolution itself does no Jedi work -- just O(1) lookups keyed by (path, line, col)."""

    def __init__(self, types: dict[tuple[str, int, int], str], calls: dict[tuple[str, int, int], str]):
        self._types = types
        self._calls = calls

    def infer_type(self, module_path: str, line: int, col: int) -> str | None:
        return self._types.get((module_path, line, col))

    def resolve_call(self, module_path: str, line: int, col: int) -> str | None:
        return self._calls.get((module_path, line, col))


def _collect_queries(module: RawModule) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """The exact positions resolution.py will query the oracle at for this module -- must
    mirror resolution's oracle calls: infer_type at each *unannotated* attribute/param name,
    resolve_call at each call. (Querying a superset of attributes is harmless -- dedup on the
    resolution side just reads back the ones it needs.)"""
    type_positions: list[tuple[int, int]] = []
    call_positions: list[tuple[int, int]] = []

    def do_func(fn: RawFunction) -> None:
        for p in fn.params:
            if p.annotation is None:
                type_positions.append((p.name_line, p.name_col))
        for c in fn.calls:
            call_positions.append((c.line, c.col))

    def do_class(cls) -> None:
        for a in cls.class_attrs:
            if a.annotation is None:
                type_positions.append((a.name_line, a.name_col))
        for method in cls.methods:
            for a in method.self_attrs:
                if a.annotation is None:
                    type_positions.append((a.name_line, a.name_col))
            do_func(method)
        for nested in cls.nested:
            do_class(nested)

    for fn in module.functions:
        do_func(fn)
    for cls in module.classes:
        do_class(cls)
    return type_positions, call_positions


def _infer_one(script: "jedi.Script", type_positions, call_positions):
    types: dict[tuple[int, int], str] = {}
    for line, col in type_positions:
        if not line:
            continue
        try:
            for d in script.infer(line, col):
                if d.name:
                    types[(line, col)] = d.name
                    break
        except Exception:
            pass
    calls: dict[tuple[int, int], tuple[str, str, str]] = {}
    for line, col in call_positions:
        if not line:
            continue
        try:
            for d in script.goto(line, col, follow_imports=True):
                if d.full_name:
                    calls[(line, col)] = (d.full_name, d.module_name, str(d.module_path))
                    break
        except Exception:
            pass
    return types, calls


def _infer_worker(payload):
    """Runs in a pool worker: answer the queries for a *batch* of files against ONE Jedi
    project, so its in-memory cross-module analysis is reused across the batch (files in a
    batch are same-package neighbours). Takes/returns only picklable primitives."""
    source_dir, batch = payload
    project = jedi.Project(path=source_dir)
    out = []
    for module_path, type_positions, call_positions in batch:
        script = jedi.Script(path=str(Path(source_dir) / module_path), project=project)
        types, calls = _infer_one(script, type_positions, call_positions)
        out.append((module_path, types, calls))
    return out


# Small contiguous batches: big enough that a worker's Jedi project caches shared imports
# across a handful of same-package files, small enough that there are many more tasks than
# workers so the pool load-balances dynamically (a few files carry very heavy inference, and
# a few huge static chunks would let one straggler gate the whole wall-time).
_BATCH_SIZE = 8


def _batches(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def build_precomputed_oracle(source_dir: str, modules: list[RawModule], workers: int | None = None) -> PrecomputedOracle:
    """Fan per-file Jedi inference across a process pool, then return a lookup-only oracle.
    `workers=1` (or a single file) runs inline, skipping pool overhead."""
    workers = workers or os.cpu_count() or 1
    per_file = [(m.path, *_collect_queries(m)) for m in modules]

    results: list = []
    if workers <= 1 or len(per_file) <= 1:
        results = _infer_worker((source_dir, per_file))
    else:
        payloads = [(source_dir, batch) for batch in _batches(per_file, _BATCH_SIZE)]
        # Preload this module (it imports jedi) into the forkserver control process so workers
        # inherit the heavy import via COW instead of re-importing it each -- see core.parallel.
        with make_process_pool(workers, preload=[__name__]) as pool:
            for batch_result in pool.map(_infer_worker, payloads):
                results.extend(batch_result)

    types: dict[tuple[str, int, int], str] = {}
    calls: dict[tuple[str, int, int], str] = {}
    # Callees are re-anchored here, in the parent: a worker returns each as Jedi named it plus
    # the file defining it, so the project-wide file map never rides along with every batch.
    dotted_by_file = _dotted_by_file(source_dir, modules)
    for module_path, file_types, file_calls in results:
        for (line, col), value in file_types.items():
            types[(module_path, line, col)] = value
        for (line, col), callee in file_calls.items():
            calls[(module_path, line, col)] = _callee_name(*callee, dotted_by_file)
    return PrecomputedOracle(types, calls)
