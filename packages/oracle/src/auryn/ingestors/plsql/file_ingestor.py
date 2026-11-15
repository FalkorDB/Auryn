"""The file-based `plsql` ingestor: parses a local directory of PL/SQL source files
into the graph (`auryn run plsql --path ...`). The git-tracked counterpart lives
in git_sync.py; both share the parse step (parse_plsql_file below) and resolution.py,
exactly as the C# ingestor shares parsing between its local-dir and git-sync paths.

Distinct from the `oracle` ingestor (ingestor.py), which reads a live Oracle
connection rather than files -- see config.py for why they root ids differently.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import time
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import click

from auryn.core.base_ingestor import BaseIngestor
from auryn.core.graph_client import GraphWriter, connect
from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.core.parallel import make_process_pool
from auryn.core.registry import register_ingestor
from auryn.ingestors.plsql import diagnostics, resolution
from auryn.ingestors.plsql.config import PLSQL_FILE_SUFFIXES, PLSQLFileConfig, project_id
from auryn.ingestors.plsql.diagnostics import Progress, SqlSyntaxError, StrictHaltError
from auryn.ingestors.plsql.raw_model import RawPlsqlFile
from auryn.ingestors.plsql.schema import FILE_EDGE_SCHEMAS, FILE_NODE_SCHEMAS
logger = logging.getLogger(__name__)


def _sa():
    """Lazily import source_analysis, which pulls the multi-megabyte generated ANTLR grammar
    (~160ms). Deferred so importing this module -- which discover_ingestors does at CLI startup to
    register the `run plsql` subcommand -- doesn't load the grammar until a parse actually runs.
    The module is cached after first import, so repeated `_sa()` calls are free."""
    import auryn.ingestors.plsql.source_analysis as sa

    return sa

# The grammar module a worker imports to parse. Preloaded into the forkserver control process so
# workers inherit the imported grammar (their per-worker IMPORT cost) via copy-on-write instead of
# re-importing the multi-megabyte generated modules.
_GRAMMAR_MODULE = "auryn.ingestors.plsql.source_analysis"

# Files a worker parses before it's recycled. Two costs pull in opposite directions:
#   * ANTLR's first PARSE in a fresh process builds its prediction-DFA cache from scratch (~5s), so a
#     worker recycled every file re-pays that 5s PER FILE -- which is exactly what crippled a real
#     300-file run (per-file `max_tasks_per_child=1` under a silently-`spawn` pool). Persistent
#     workers pay it once, then reuse the cache (~0.6s/file).
#   * But that same DFA cache grows unbounded with the huge generated calc packages, eventually
#     OOM-killing a never-recycled worker (observed ~375 files in).
# Recycling every 50 files gets the amortised startup of persistent workers while capping the cache
# at ~50 files' growth -- an order of magnitude under the observed OOM point.
_RECYCLE_EVERY = 50


def _parser_engine(strict: bool) -> str:
    """A one-line description of which parser will actually run, so a run's logs make it
    unambiguous whether the fast C++ accelerator (`plsql_accel`) is in play or we fell back to the
    pure-Python ANTLR runtime (~10x slower on real PL/SQL). The accelerator is used only by the
    best-effort path; --strict always parses in pure Python (see parse_declarations)."""
    if strict:
        if _sa().accelerator_available():
            return "pure-Python ANTLR (C++ accelerator 'plsql_accel' is installed but NOT used in --strict mode)"
        return "pure-Python ANTLR ('plsql_accel' not installed)"
    if _sa().accelerator_available():
        return "C++ accelerator 'plsql_accel' (fast path)"
    return "pure-Python ANTLR ('plsql_accel' not installed -- expect ~10x slower; build tools/plsql-accel to speed up)"


def _make_pool(workers: int) -> ProcessPoolExecutor:
    """Build the parse pool, sidestepping two traps that made a real 300-file run crawl:

    1. `max_tasks_per_child` is INCOMPATIBLE with the `fork` start method (make_process_pool skips
       the `fork` fallback when it's set, going forkserver -> spawn), and passing it with no explicit
       context makes ProcessPoolExecutor silently fall back to `spawn` -- where every worker
       re-imports the multi-megabyte grammar. Preloading `_GRAMMAR_MODULE` into the forkserver
       control process means workers fork warm (import inherited via COW) instead.
    2. Recycling every file (the old policy) re-pays ANTLR's ~5s first-parse DFA build per file. We
       recycle every _RECYCLE_EVERY files instead, so that cost amortises while memory stays bounded.
    """
    return make_process_pool(
        workers, preload=[_GRAMMAR_MODULE], max_tasks_per_child=_RECYCLE_EVERY
    )


class _ParseTimeout(BaseException):
    """Raised from the per-file SIGALRM handler. A BaseException (not Exception) so the parser's
    broad `except Exception` recovery can't swallow it -- only parse_plsql_file catches it."""


def parse_plsql_file(path: str, max_seconds: int | None = None) -> RawPlsqlFile:
    """Parse one PL/SQL file into a RawPlsqlFile. A module-level function (not a
    method/closure) so ProcessPoolExecutor can pickle it to workers. Reads bytes and
    decodes leniently -- legacy PL/SQL is often not UTF-8, and a best-effort parser
    shouldn't die on a stray byte.

    `max_seconds` caps the parse of a single file (SIGALRM, Unix only): a pathological file that
    exceeds it is skipped (returned empty with `timed_out=True`) instead of stalling the whole
    run. This bounds total wall-clock -- the ANTLR Python runtime can spend tens of seconds on
    the largest generated calc packages."""
    source = Path(path).read_bytes().decode("utf-8", errors="replace")
    if max_seconds and hasattr(signal, "SIGALRM"):
        def _on_timeout(signum, frame):
            raise _ParseTimeout  # BaseException -> the parser's `except Exception` can't swallow it

        previous = signal.signal(signal.SIGALRM, _on_timeout)
        signal.alarm(int(max_seconds))
        try:
            return _sa().parse_declarations(source, path)
        except _ParseTimeout:
            skipped = RawPlsqlFile(path=path)
            skipped.timed_out = True
            return skipped
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)
    return _sa().parse_declarations(source, path)


def _parse_one_strict(args: tuple[str, tuple[str, ...]]):
    """ProcessPoolExecutor worker for the parallel strict path: decode + strict-parse ONE file,
    returning a picklable result tuple instead of raising, so the parent can pick the first
    failure in file order. Module-level (picklable). Result tuples:
        ("ok", RawPlsqlFile, encoding)
        ("encoding", raw_bytes, UnicodeDecodeError)
        ("parse", text, encoding, SqlSyntaxError)   # SqlSyntaxError is picklable (see __reduce__)
    """
    path, encodings = args
    raw = Path(path).read_bytes()
    try:
        text, encoding = _decode(raw, encodings)
    except UnicodeDecodeError as error:
        return ("encoding", raw, error)
    try:
        raw_file = _sa().parse_declarations(text, path, strict=True)
    except SqlSyntaxError as error:
        return ("parse", text, encoding, error)
    return ("ok", raw_file, encoding)


def _decode(raw: bytes, encodings: tuple[str, ...]) -> tuple[str, str]:
    """Decode `raw` by trying `encodings` in order; return (text, encoding_used). Raises the
    last UnicodeDecodeError if none succeed (only reachable when even latin-1 is excluded --
    latin-1 maps every byte, so the default chain never fails)."""
    last_error: UnicodeDecodeError | None = None
    for encoding in encodings:
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError as error:
            last_error = error
    raise last_error  # type: ignore[misc]


def _object_counts(raw_file: RawPlsqlFile) -> dict[str, int]:
    """Per-object-type tally of what one file contributed -- surfaced in the run summary and,
    on a strict halt, in the diagnostic's 'how far did it get' progress."""
    return {
        "packages": len(raw_file.packages),
        "routines": len(raw_file.routines),
        "triggers": len(raw_file.triggers),
        "types": len(raw_file.types),
        "tables": len(raw_file.tables),
        "views": len(raw_file.views),
        "indexes": len(raw_file.indexes),
        "sequences": len(raw_file.sequences),
        "alterations": len(raw_file.alterations),
    }


@register_ingestor("plsql")
class PLSQLFileIngestor(BaseIngestor):
    node_schemas = FILE_NODE_SCHEMAS
    edge_schemas = FILE_EDGE_SCHEMAS

    def __init__(self, config: PLSQLFileConfig) -> None:
        self._config = config

    @classmethod
    def build_cli_command(cls) -> click.Command:
        @click.command(
            "plsql",
            help="Ingest a directory of PL/SQL source files (packages, procedures, functions, triggers, types).",
        )
        @click.option(
            "--path",
            required=False,
            default=None,
            type=click.Path(exists=True, file_okay=False),
            help="Root directory to scan for PL/SQL files (recursively). Required unless "
            "--files-from is given, in which case it's the (optional) id root the listed files "
            "are made relative to.",
        )
        @click.option(
            "--files-from",
            "files_from",
            default=None,
            type=click.File("r"),
            help="Ingest exactly the files listed here (one path per line; '-' reads stdin), instead "
            "of scanning --path by suffix. Bypasses the suffix filter, so PL/SQL in odd-extension "
            "files is ingested too. Composes with grep, e.g. `grep -rl 'create ... package' src | "
            "auryn run plsql --files-from - --project-name P --path src`.",
        )
        @click.option("--project-name", required=True, help="Logical name for this project (builds node ids).")
        @click.option("--solution-name", default=None, help="Optional: wraps the Project in a Solution node.")
        @click.option("--workers", default=None, type=int, help="Parallel per-file parsing. Default: os.cpu_count().")
        @click.option(
            "--strict/--no-strict",
            default=True,
            help="Fail-fast: halt on the first file that won't decode/parse, writing a full "
            "diagnostic report BEFORE anything is written to the graph (default: on). Use "
            "--no-strict for best-effort (skip unparseable files and continue).",
        )
        @click.option(
            "--diagnostics-dir",
            default=None,
            type=click.Path(file_okay=False),
            help="Where strict-mode diagnostic reports are written. Default: "
            "<cwd>/auryn-diagnostics.",
        )
        @click.option(
            "--max-parse-seconds",
            default=None,
            type=int,
            help="Best-effort (--no-strict) only: per-file parse timeout (Unix). A pathological "
            "file that exceeds it is skipped and reported, so one slow file can't stall the run. "
            "Default: no cap.",
        )
        @click.pass_context
        def command(
            ctx: click.Context,
            path: str | None,
            files_from,
            project_name: str,
            solution_name: str | None,
            workers: int | None,
            strict: bool,
            diagnostics_dir: str | None,
            max_parse_seconds: int | None,
        ) -> None:
            explicit_files: tuple[str, ...] | None = None
            if files_from is not None:
                explicit_files = tuple(
                    line.strip() for line in files_from.read().splitlines() if line.strip()
                )
                if not explicit_files:
                    raise click.UsageError("--files-from produced an empty list (no file paths on input).")
            if path is None and explicit_files is None:
                raise click.UsageError(
                    "Provide --path (scan a directory) or --files-from (an explicit list of files)."
                )
            # id root: --path if given, else the common ancestor of the listed files' directories
            # (their dirnames, so a single listed file roots at its own folder, not itself).
            root = path or os.path.commonpath([os.path.dirname(os.path.abspath(f)) for f in explicit_files])

            writer = GraphWriter(connect(ctx.obj["falkordb_config"]))
            config = PLSQLFileConfig(
                path=root,
                project_name=project_name,
                solution_name=solution_name,
                workers=workers,
                strict=strict,
                diagnostics_dir=diagnostics_dir,
                max_parse_seconds=max_parse_seconds,
                files=explicit_files,
            )
            try:
                stats = cls(config).run(writer)
            except StrictHaltError as halt:
                click.echo(f"\nHALTED (strict mode): {halt}", err=True)
                if halt.report_path is not None:
                    click.echo(f"Diagnostic written to: {halt.report_path}", err=True)
                    click.echo(f"  (and {halt.report_path.with_suffix('.txt')})", err=True)
                click.echo("Nothing was written to the graph. Fix or exclude the file above and re-run.", err=True)
                raise SystemExit(1) from halt
            click.echo(f"Merged {stats.nodes_merged} nodes, {stats.edges_merged} edges in {stats.elapsed_seconds:.2f}s")

        return command

    def extract(self) -> Iterator[GraphNode | GraphEdge]:
        root = Path(self._config.path).resolve()
        if self._config.files is not None:
            files = self._resolve_explicit_files(root)
            logger.info("Ingesting %d explicitly listed file(s), rooted at %s", len(files), root)
        else:
            files = self._discover_files(root)
            logger.info("Discovered %d SQL/PL-SQL file(s) to ingest under %s", len(files), root)

        logger.info("PL/SQL parser engine: %s", _parser_engine(self._config.strict))

        # Parsing happens fully here, before the first yield -- so a strict halt (raised from
        # _parse_files_strict) stops the run before BaseIngestor.run() merges anything.
        if self._config.strict:
            raw_files = self._parse_files_strict(files, root)
        else:
            raw_files = self._parse_files(files, root)

        proj_id = project_id(self._config.project_name, self._config.solution_name)
        yield GraphNode("Project", {"id": proj_id}, {"name": self._config.project_name, "provenance": "source"})
        if self._config.solution_name:
            solution_id = f"plsql://{self._config.solution_name}"
            yield GraphNode("Solution", {"id": solution_id}, {"name": self._config.solution_name})
            yield GraphEdge("HAS_PROJECT", "Solution", {"id": solution_id}, "Project", {"id": proj_id})

        yield from resolution.resolve(raw_files, proj_id)

    @staticmethod
    def _discover_files(root: Path) -> list[Path]:
        return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in PLSQL_FILE_SUFFIXES)

    def _resolve_explicit_files(self, root: Path) -> list[Path]:
        """Resolve the `--files-from` list to a sorted, de-duplicated set of files under `root`.
        Deliberately NOT suffix-filtered -- the caller hand-picked these (e.g. via grep), so a
        CREATE PACKAGE/FUNCTION living in a non-standard extension is ingested. Non-files and files
        outside `root` (which couldn't be keyed relative to the id root) are skipped with a warning
        rather than aborting the run."""
        resolved: set[Path] = set()
        for entry in self._config.files or ():
            path = Path(entry)
            if not path.is_absolute():
                path = root / path
            path = path.resolve()
            if not path.is_file():
                logger.warning("--files-from: skipping (not a file): %s", entry)
                continue
            try:
                path.relative_to(root)
            except ValueError:
                logger.warning("--files-from: skipping (outside id root %s): %s", root, entry)
                continue
            resolved.add(path)
        return sorted(resolved)

    def _parse_files(self, files: list[Path], root: Path) -> list[RawPlsqlFile]:
        workers = self._config.workers or os.cpu_count() or 1
        cap = self._config.max_parse_seconds
        paths = [str(f) for f in files]
        total = len(paths)
        raw_files: list[RawPlsqlFile] = []
        if workers == 1 or total <= 1:
            for index, path in enumerate(paths, start=1):
                logger.info("[%d/%d] %s", index, total, Path(path).relative_to(root).as_posix())
                raw_files.append(parse_plsql_file(path, cap))
        else:
            # Parallel: submit all, log a per-file counter as each one FINISHES (out of order,
            # so we show the file that just completed, not a "current file"). Results are kept in
            # submission order via the index so downstream is unchanged.
            ordered: list[RawPlsqlFile | None] = [None] * total
            done = 0
            try:
                with _make_pool(workers) as pool:
                    futures = {pool.submit(parse_plsql_file, path, cap): i for i, path in enumerate(paths)}
                    for future in as_completed(futures):
                        index = futures[future]
                        ordered[index] = future.result()
                        done += 1
                        logger.info("[%d/%d] %s", done, total, Path(paths[index]).relative_to(root).as_posix())
            except BrokenProcessPool:
                # A worker died (OOM/segfault on a pathological file). Best-effort: don't crash --
                # keep everything that finished and report what didn't.
                missing = [Path(paths[i]).relative_to(root).as_posix() for i in range(total) if ordered[i] is None]
                logger.error(
                    "A worker process died (likely OOM on a pathological file). Keeping the %d file(s) "
                    "that parsed; %d not parsed. To capture those too, re-run with a cap, e.g. "
                    "--max-parse-seconds 60 (and/or --workers 2). Unparsed: %s",
                    done, len(missing), ", ".join(missing[:15]) + (" ..." if len(missing) > 15 else ""),
                )
            raw_files = [rf for rf in ordered if rf is not None]
        for raw_file in raw_files:
            raw_file.path = Path(raw_file.path).relative_to(root).as_posix()
        self._report_timeouts(raw_files)
        self._report_grammar_gaps(raw_files)
        return raw_files

    def _report_timeouts(self, raw_files: list[RawPlsqlFile]) -> None:
        """Surface files skipped by --max-parse-seconds (empty result) so they're visible."""
        timed_out = [rf.path for rf in raw_files if rf.timed_out]
        if timed_out:
            logger.warning(
                "TIMED OUT: %d file(s) exceeded --max-parse-seconds=%s and were skipped (empty): %s",
                len(timed_out), self._config.max_parse_seconds, ", ".join(timed_out),
            )

    def _report_grammar_gaps(self, raw_files: list[RawPlsqlFile]) -> None:
        """Best-effort mode: surface files where ANTLR recovered from a grammar gap (a construct
        the vendored grammar can't parse). The rest of each such file was still extracted; this
        just makes the gaps visible (log + a machine-readable grammar-gaps.json to send back)
        instead of silent."""
        gaps = [rf for rf in raw_files if rf.parse_errors]
        if not gaps:
            logger.info("Best-effort parse: all %d file(s) parsed cleanly (no grammar gaps).", len(raw_files))
            return
        total = sum(rf.parse_errors for rf in gaps)
        logger.warning(
            "Best-effort parse: %d/%d file(s) hit grammar gaps -- %d construct(s) skipped via recovery; "
            "the rest of each file WAS extracted.", len(gaps), len(raw_files), total,
        )
        for rf in sorted(gaps, key=lambda r: r.parse_errors, reverse=True)[:20]:
            line = rf.first_error[0] if rf.first_error else "?"
            logger.warning("  %s -- %d construct(s) skipped, first at line %s", rf.path, rf.parse_errors, line)

        diagnostics_dir = self._config.diagnostics_dir or str(Path.cwd() / "auryn-diagnostics")
        try:
            Path(diagnostics_dir).mkdir(parents=True, exist_ok=True)
            report = {
                "files_with_gaps": len(gaps),
                "total_constructs_skipped": total,
                "files": [
                    {
                        "file": rf.path,
                        "skipped": rf.parse_errors,
                        "first_error_line": rf.first_error[0] if rf.first_error else None,
                        # the actual offending constructs (one per distinct line, capped) so each
                        # gap class can be fixed straight from this report:
                        "samples": [{"line": line, "source": text} for line, text in rf.error_samples],
                    }
                    for rf in sorted(gaps, key=lambda r: r.parse_errors, reverse=True)
                ],
            }
            out = Path(diagnostics_dir) / "grammar-gaps.json"
            out.write_text(json.dumps(report, indent=2), encoding="utf-8")
            logger.warning("Grammar-gap report written to %s (send this back to close the gaps)", out)
        except OSError as error:
            logger.warning("Could not write grammar-gap report: %s", error)

    def _parse_files_strict(self, files: list[Path], root: Path) -> list[RawPlsqlFile]:
        """Fail-fast parse for a first ingestion of unfamiliar data. The first file that won't
        decode or parse writes a full diagnostic and raises StrictHaltError -- halting the whole
        run before any graph write. Parallel across cores by default (files parse concurrently,
        then the first failure *in file order* is reported, preserving determinism); `--workers 1`
        forces the sequential path with its per-file progress line."""
        diagnostics_dir = self._config.diagnostics_dir or str(Path.cwd() / "auryn-diagnostics")
        workers = self._config.workers or os.cpu_count() or 1
        logger.warning(
            "STRICT mode: halt on the first decode/parse failure; diagnostics -> %s (%d worker(s))",
            diagnostics_dir, workers,
        )
        if workers == 1 or len(files) <= 1:
            return self._parse_files_strict_sequential(files, root, diagnostics_dir)
        return self._parse_files_strict_parallel(files, root, diagnostics_dir, workers)

    def _parse_files_strict_sequential(self, files: list[Path], root: Path, diagnostics_dir: str) -> list[RawPlsqlFile]:
        raw_files: list[RawPlsqlFile] = []
        object_totals: Counter[str] = Counter()
        unmodeled_totals: Counter[str] = Counter()
        encodings_used: Counter[str] = Counter()

        total = len(files)
        width = len(str(total))
        started_run = time.monotonic()

        for index, file in enumerate(files, start=1):
            rel = file.relative_to(root).as_posix()
            raw_bytes = file.read_bytes()
            # Progress line BEFORE parsing, so the current file is visible while it's being
            # worked on (some CatsAndCalcs files parse for seconds) -- not just after it's done.
            logger.info("[%*d/%d] %s (%.1f KB)", width, index, total, rel, len(raw_bytes) / 1024)
            started_file = time.monotonic()
            progress = Progress(
                files_scanned=len(files), files_parsed_ok=len(raw_files), objects_extracted=dict(object_totals)
            )

            try:
                text, encoding = _decode(raw_bytes, self._config.encodings)
            except UnicodeDecodeError as error:
                report = diagnostics.build_encoding_report(
                    file_path=rel, raw=raw_bytes, tried=list(self._config.encodings), decode_error=error, progress=progress
                )
                report_path = diagnostics.write_report(diagnostics_dir, report, index)
                logger.error(
                    "ENCODING FAILURE on %s -- halting. Diagnostic: %s\n%s",
                    rel, report_path, diagnostics.render_text(report),
                )
                raise StrictHaltError(f"encoding failure on {rel}", report, report_path) from error

            if encoding != "utf-8":
                encodings_used[encoding] += 1
                logger.warning("%s decoded as %s (not UTF-8) -- recorded, continuing", rel, encoding)

            try:
                raw_file = _sa().parse_declarations(text, str(file), strict=True)
            except SqlSyntaxError as error:
                report = diagnostics.build_failure_report(
                    file_path=rel, source=text, encoding=encoding, error=error, progress=progress
                )
                report_path = diagnostics.write_report(diagnostics_dir, report, index)
                logger.error(
                    "PARSE FAILURE on %s at line %d:%d (%s) -- halting. Diagnostic: %s\n%s",
                    rel, error.line, error.column, report.get("likely_dialect"), report_path,
                    diagnostics.render_text(report),
                )
                raise StrictHaltError(
                    f"parse failure on {rel} (line {error.line}:{error.column})", report, report_path
                ) from error

            raw_file.path = rel
            raw_files.append(raw_file)
            object_totals.update(_object_counts(raw_file))
            unmodeled_totals.update(_sa().unmodeled_statement_kinds(raw_file))

            elapsed_file = time.monotonic() - started_file
            if elapsed_file >= 3.0:  # surface the slow files (usually the big calc packages)
                logger.info("%*s   └ %s took %.1fs", width, "", rel, elapsed_file)

        logger.info(
            "STRICT parse OK: %d/%d file(s) parsed in %.1fs. Objects extracted: %s",
            len(raw_files), len(files), time.monotonic() - started_run, dict(object_totals),
        )
        if encodings_used:
            logger.warning("Non-UTF-8 files decoded via fallback: %s", dict(encodings_used))
        if unmodeled_totals:
            logger.warning(
                "COVERAGE GAP -- statement kinds parsed but NOT modelled (no graph nodes emitted): %s",
                dict(unmodeled_totals),
            )
        return raw_files

    def _parse_files_strict_parallel(
        self, files: list[Path], root: Path, diagnostics_dir: str, workers: int
    ) -> list[RawPlsqlFile]:
        """Parse every file concurrently, then walk the results in file order and halt at the
        first failure -- so a strict run keeps its deterministic "first bad file" diagnostic and
        its nothing-written-on-failure guarantee, but at ~cores× the speed. Workers return
        picklable result tuples (never raise) so the parent can attribute the failure precisely."""
        total = len(files)
        started_run = time.monotonic()
        args = [(str(f), self._config.encodings) for f in files]
        results: list = [None] * total
        done = 0
        try:
            with _make_pool(workers) as pool:
                futures = {pool.submit(_parse_one_strict, arg): i for i, arg in enumerate(args)}
                for future in as_completed(futures):
                    index = futures[future]
                    results[index] = future.result()
                    done += 1
                    logger.info("[%d/%d] %s", done, total, files[index].relative_to(root).as_posix())
        except BrokenProcessPool as error:
            # A worker died (OOM/segfault). Strict can't guarantee "first failure in order" with a
            # gap, so stop cleanly with guidance rather than a traceback -- best-effort survives this.
            unfinished = sum(1 for r in results if r is None)
            raise StrictHaltError(
                f"a parser worker died mid-run (likely OOM); {unfinished} file(s) unparsed. "
                "Re-run with --no-strict (resilient, keeps going) and/or --max-parse-seconds 60 "
                "and --workers 2 to bound memory.",
                {"kind": "worker_died", "unfinished": unfinished},
                None,
            ) from error

        raw_files: list[RawPlsqlFile] = []
        object_totals: Counter[str] = Counter()
        unmodeled_totals: Counter[str] = Counter()
        encodings_used: Counter[str] = Counter()

        for index, result in enumerate(results):
            rel = files[index].relative_to(root).as_posix()
            progress = Progress(
                files_scanned=total, files_parsed_ok=len(raw_files), objects_extracted=dict(object_totals)
            )
            kind = result[0]
            if kind == "ok":
                _, raw_file, encoding = result
                if encoding != "utf-8":
                    encodings_used[encoding] += 1
                raw_file.path = rel
                raw_files.append(raw_file)
                object_totals.update(_object_counts(raw_file))
                unmodeled_totals.update(_sa().unmodeled_statement_kinds(raw_file))
            elif kind == "encoding":
                _, raw_bytes, error = result
                report = diagnostics.build_encoding_report(
                    file_path=rel, raw=raw_bytes, tried=list(self._config.encodings), decode_error=error, progress=progress
                )
                report_path = diagnostics.write_report(diagnostics_dir, report, index + 1)
                logger.error("ENCODING FAILURE on %s -- halting. Diagnostic: %s\n%s", rel, report_path, diagnostics.render_text(report))
                raise StrictHaltError(f"encoding failure on {rel}", report, report_path)
            else:  # "parse"
                _, text, encoding, error = result
                report = diagnostics.build_failure_report(
                    file_path=rel, source=text, encoding=encoding, error=error, progress=progress
                )
                report_path = diagnostics.write_report(diagnostics_dir, report, index + 1)
                logger.error(
                    "PARSE FAILURE on %s at line %d:%d (%s) -- halting. Diagnostic: %s\n%s",
                    rel, error.line, error.column, report.get("likely_dialect"), report_path, diagnostics.render_text(report),
                )
                raise StrictHaltError(f"parse failure on {rel} (line {error.line}:{error.column})", report, report_path)

        logger.info(
            "STRICT parse OK: %d/%d file(s) parsed in %.1fs across %d workers. Objects extracted: %s",
            len(raw_files), total, time.monotonic() - started_run, workers, dict(object_totals),
        )
        if encodings_used:
            logger.warning("Non-UTF-8 files decoded via fallback: %s", dict(encodings_used))
        if unmodeled_totals:
            logger.warning("COVERAGE GAP -- statement kinds parsed but NOT modelled: %s", dict(unmodeled_totals))
        return raw_files
