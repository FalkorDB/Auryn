"""`auryn run oraschema` -- reconstruct an Oracle schema catalog from a directory of
per-object DDL scripts (a Redgate "Schema Compare for Oracle" scripts-folder export),
no live connection.

Layout it expects (case-insensitive on the object folders):

    <root>/
      DatabaseInformation.xml            # UTF-16: <DatabaseVersion>NineteenC</...>
      <SCHEMA>/
        SchemaInformation.xml            # UTF-16: <Name>MYSCHEMA</Name>
        Tables/*.sql  Views/*.sql  Indexes/*.sql  Sequences/*.sql   # UTF-8, one CREATE each

Every `.sql` under a schema folder is parsed by the shared vendored ANTLR grammar
(source_analysis.parse_declarations) -- the object-folder names are not trusted, the
parse decides what each file is -- and resolved onto the live `sql` ingestor's
Database/Schema/Table/... id scheme so the result unifies with the same database's
code graph. All parsing happens before the first node is emitted, so `--strict` halts
the run before anything is written to the graph.
"""

from __future__ import annotations

import logging
import os
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
from auryn.ingestors.oraschema import metadata, resolution
from auryn.ingestors.oraschema.config import OBJECT_FILE_SUFFIXES, OraSchemaConfig
from auryn.ingestors.oraschema.schema import EDGE_SCHEMAS, NODE_SCHEMAS
from auryn.ingestors.plsql.diagnostics import (
    Progress,
    SqlSyntaxError,
    StrictHaltError,
    build_failure_report,
    write_report,
)
# Reuse the plsql ingestor's parse worker verbatim: parse_plsql_file (decode + per-file SIGALRM
# cap + parse_declarations, module-level so it pickles to workers). The pool policy is our own --
# see _make_pool: unlike plsql's giant hand-written packages, schema-object files are small, so we
# recycle workers every N files (not every file) to amortise startup, and prefer a fork context.
from auryn.ingestors.plsql.file_ingestor import parse_plsql_file
from auryn.ingestors.plsql.raw_model import RawPlsqlFile
def _sa():
    """Lazily import the plsql source_analysis (pulls the generated ANTLR grammar, ~160ms) so
    importing this module at CLI startup -- discover_ingestors, for the `run oraschema` subcommand
    -- doesn't load the grammar until a parse actually runs. Cached after first import."""
    import auryn.ingestors.plsql.source_analysis as sa

    return sa

logger = logging.getLogger("auryn.oraschema")

# Object folders a Redgate export writes for the *catalog* objects this ingestor models.
# Used only to recognise a directory as a schema; the actual object type comes from parsing
# each file, so a schema whose objects sit loose (no subfolders) still works.
_OBJECT_FOLDERS = ("tables", "views", "indexes", "sequences")

_PROGRESS_EVERY = 200  # emit a parse-progress line every N files

# Below this file count, a single schema parses inline (no pool): process-pool spin-up + IPC
# isn't worth it for a handful of files (and keeps the extract() unit tests fork-free/fast).
_PARALLEL_THRESHOLD = 64

# The grammar module a pool worker must import to parse. Preloaded into the forkserver control
# process so forked workers inherit the (multi-megabyte, slow-to-deserialize) ANTLR ATN instead of
# each re-importing it -- this is what makes the pool start fast.
_GRAMMAR_MODULE = "auryn.ingestors.plsql.source_analysis"


def _make_pool(workers: int) -> ProcessPoolExecutor:
    """A parse pool with long-lived workers, preloading the grammar ATN into the forkserver control
    process so workers inherit it via copy-on-write (see core.parallel.make_process_pool).

    Deliberately NO max_tasks_per_child: schema-object files are small (unlike plsql's giant
    packages) so long-lived workers stay memory-bounded without per-file recycling."""
    return make_process_pool(workers, preload=[_GRAMMAR_MODULE])


@register_ingestor("oraschema")
class OraSchemaIngestor(BaseIngestor):
    node_schemas = NODE_SCHEMAS
    edge_schemas = EDGE_SCHEMAS

    def __init__(self, config: OraSchemaConfig, diagnostics_dir: str | None = None) -> None:
        self._config = config
        self._diagnostics_dir = diagnostics_dir

    @classmethod
    def build_cli_command(cls) -> click.Command:
        @click.command(
            "oraschema",
            help=(
                "Reconstruct an Oracle schema catalog (tables, columns, constraints, "
                "foreign keys, indexes, sequences, views) from a directory of per-object "
                "DDL scripts -- e.g. a Redgate 'Schema Compare for Oracle' export. No "
                "database connection required."
            ),
        )
        @click.option(
            "--path",
            required=True,
            type=click.Path(exists=True, file_okay=False),
            help="Export root: holds DatabaseInformation.xml and the per-schema folder(s).",
        )
        @click.option(
            "--database-name",
            required=True,
            help="Logical database name; root of every id (oracle://<database-name>...). "
            "Match the live sql/oracle ingestor's --database-name to unify with the code graph.",
        )
        @click.option(
            "--strict/--no-strict",
            default=False,
            help="Fail-fast: halt on the first .sql that won't parse, writing a diagnostic "
            "BEFORE anything is merged. Default --no-strict: log parse gaps and reconstruct "
            "as much of the catalog as parses.",
        )
        @click.option(
            "--diagnostics-dir",
            default=None,
            type=click.Path(file_okay=False),
            help="Where a --strict halt report is written. Default: <cwd>/auryn-diagnostics.",
        )
        @click.option(
            "--workers",
            default=None,
            type=int,
            help="Best-effort only: parallel parse workers (default: CPU count). The view SELECTs "
            "dominate wall-clock, so parsing across cores is the main speed lever. Ignored under "
            "--strict (single-worker for a deterministic halt).",
        )
        @click.option(
            "--max-parse-seconds",
            default=None,
            type=int,
            help="Best-effort only: per-file parse timeout (Unix). A pathological file (usually a "
            "giant generated view) that exceeds it is skipped and reported, so one file can't stall "
            "the run. Default: no cap.",
        )
        @click.pass_context
        def command(
            ctx: click.Context,
            path: str,
            database_name: str,
            strict: bool,
            diagnostics_dir: str | None,
            workers: int | None,
            max_parse_seconds: int | None,
        ) -> None:
            writer = GraphWriter(connect(ctx.obj["falkordb_config"]))
            config = OraSchemaConfig(
                path=path,
                database_name=database_name,
                strict=strict,
                workers=workers,
                max_parse_seconds=max_parse_seconds,
            )
            try:
                stats = cls(config, diagnostics_dir=diagnostics_dir).run(writer)
            except StrictHaltError as halt:
                click.echo(f"\nHALTED (strict mode): {halt}", err=True)
                if halt.report_path is not None:
                    click.echo(f"Diagnostic written to: {halt.report_path}", err=True)
                click.echo(
                    "Nothing was written to the graph. Fix or exclude the file above and re-run.",
                    err=True,
                )
                raise SystemExit(1) from halt
            click.echo(
                f"Merged {stats.nodes_merged} nodes, {stats.edges_merged} edges "
                f"in {stats.elapsed_seconds:.2f}s"
            )

        return command

    def extract(self) -> Iterator[GraphNode | GraphEdge]:
        root = Path(self._config.path).resolve()
        database_id = f"oracle://{self._config.database_name}"
        version = metadata.read_database_version(root)

        schema_dirs = _discover_schemas(root)
        if not schema_dirs:
            logger.warning("No schema folder (or object .sql files) found under %s", root)

        # Phase 1 -- parse everything up front, so a --strict halt raises here, before the
        # first yield below and therefore before BaseIngestor.run() merges anything.
        parsed: list[tuple[str, str, list[RawPlsqlFile]]] = []
        for schema_dir in schema_dirs:
            schema_name = (metadata.read_schema_name(schema_dir) or schema_dir.name).upper()
            schema_id = f"{database_id}.{schema_name}"
            raw_files = self._parse_schema(schema_dir, schema_name, root)
            parsed.append((schema_id, schema_name, raw_files))

        # Phase 2 -- emit. Database root first, then each schema's subgraph.
        yield GraphNode(
            "Database",
            {"id": database_id},
            {
                "name": self._config.database_name,
                "dialect": "oracle",
                "version": version,
                "provenance": "source",
            },
        )
        for schema_id, schema_name, raw_files in parsed:
            yield GraphNode("Schema", {"id": schema_id}, {"name": schema_name, "provenance": "source"})
            yield GraphEdge(
                "HAS_SCHEMA", "Database", {"id": database_id}, "Schema", {"id": schema_id}
            )
            yield from resolution.resolve_schema(raw_files, schema_id)

    # -- parsing --------------------------------------------------------------------

    def _parse_schema(self, schema_dir: Path, schema_name: str, root: Path) -> list[RawPlsqlFile]:
        files = sorted(
            p for p in schema_dir.rglob("*") if p.is_file() and p.suffix.lower() in OBJECT_FILE_SUFFIXES
        )
        logger.info("Schema %s: parsing %d object .sql file(s)", schema_name, len(files))

        # Pass 1 -- sqlglot (pure-Python, fast, no process pool). Files it can't parse (Oracle
        # physical-storage DDL) are collected for the ANTLR fallback so coverage never regresses.
        raw_files, fallback = self._sqlglot_pass(files, schema_name, root)
        logger.info(
            "Schema %s: sqlglot parsed %d/%d file(s)%s",
            schema_name, len(raw_files), len(files),
            f", {len(fallback)} fall back to ANTLR" if fallback else "",
        )
        # Pass 2 -- ANTLR only for the fallback files (strict halts here; best-effort uses the pool).
        if fallback:
            raw_files.extend(self._antlr_pass(fallback, schema_name, root))
        return raw_files

    def _sqlglot_pass(
        self, files: list[Path], schema_name: str, root: Path
    ) -> tuple[list[RawPlsqlFile], list[Path]]:
        # Deferred: sqlglot is a heavy import (~23ms) only needed when actually parsing DDL, not at
        # CLI startup when discover_ingestors imports this module to register the subcommand.
        from auryn.ingestors.oraschema import sqlglot_parse

        raw_files: list[RawPlsqlFile] = []
        fallback: list[Path] = []
        for index, path in enumerate(files, start=1):
            rel = str(path.relative_to(root))
            try:
                text, _encoding = _decode(path.read_bytes(), self._config.encodings)
                raw = sqlglot_parse.parse_ddl_file(text, rel)
            except sqlglot_parse.SqlglotUnsupported:
                fallback.append(path)
                continue
            raw.path = rel
            raw_files.append(raw)
            if index % _PROGRESS_EVERY == 0:
                logger.info("Schema %s: parsed %d/%d files (sqlglot)", schema_name, index, len(files))
        return raw_files, fallback

    def _antlr_pass(self, files: list[Path], schema_name: str, root: Path) -> list[RawPlsqlFile]:
        # Strict is single-worker on purpose: it halts on the first failure in file order, which
        # only stays deterministic (and the diagnostic coherent) if files are parsed in sequence.
        if self._config.strict:
            logger.info("Schema %s: ANTLR fallback for %d file(s) (strict, single worker)",
                        schema_name, len(files))
            return self._parse_strict(files, schema_name, root)

        workers = self._effective_workers(len(files))
        cap = f", {self._config.max_parse_seconds}s/file cap" if self._config.max_parse_seconds else ""
        logger.info("Schema %s: ANTLR fallback for %d file(s) (%d worker%s%s)",
                    schema_name, len(files), workers, "" if workers == 1 else "s", cap)
        pairs = (
            self._parse_parallel(files, root, workers, schema_name)
            if workers > 1
            else self._parse_inline(files, root)
        )
        return self._collect(pairs, len(files), schema_name)

    def _effective_workers(self, file_count: int) -> int:
        """CPU count by default; forced to 1 for a small schema so the pool's spin-up + IPC
        overhead never exceeds the parse it's meant to speed up."""
        if file_count < _PARALLEL_THRESHOLD:
            return 1
        requested = self._config.workers or (os.cpu_count() or 1)
        return max(1, requested)

    def _parse_inline(self, files: list[Path], root: Path):
        """Sequential best-effort parse -- one worker, no pool. Still honours the per-file cap."""
        for path in files:
            yield str(path.relative_to(root)), parse_plsql_file(str(path), self._config.max_parse_seconds)

    def _parse_parallel(self, files: list[Path], root: Path, workers: int, schema_name: str):
        """Best-effort parse across a process pool (fresh worker per file, bounding ANTLR's
        per-process DFA-cache growth). Yields (rel_path, RawPlsqlFile) as each file finishes --
        completion order, not file order, which only affects the order gaps are logged."""
        with _make_pool(workers) as pool:
            future_to_path = {
                pool.submit(parse_plsql_file, str(path), self._config.max_parse_seconds): path
                for path in files
            }
            try:
                for future in as_completed(future_to_path):
                    path = future_to_path[future]
                    yield str(path.relative_to(root)), future.result()
            except BrokenProcessPool:
                # A worker died (e.g. OOM-killed). With a fresh worker per file this is rare, but if
                # it happens the remaining futures are lost -- keep the partial results rather than
                # abandoning the whole schema (best-effort). Strict is the path that must be exact.
                logger.error(
                    "Schema %s: worker pool crashed mid-parse; keeping partial results", schema_name
                )

    def _collect(self, pairs, total: int, schema_name: str) -> list[RawPlsqlFile]:
        """Drain (rel, RawPlsqlFile) pairs, re-stamping the relative path and tracking progress,
        parse gaps and timeouts for the run summary."""
        raw_files: list[RawPlsqlFile] = []
        gaps: list[tuple[str, int, str]] = []
        timed_out: list[str] = []
        done = 0
        for rel, raw in pairs:
            raw.path = rel
            raw_files.append(raw)
            done += 1
            if raw.timed_out:
                timed_out.append(rel)
            elif raw.parse_errors and raw.error_samples:
                line, sample = raw.error_samples[0]
                gaps.append((rel, line, sample))
            if done % _PROGRESS_EVERY == 0:
                logger.info("Schema %s: parsed %d/%d files", schema_name, done, total)

        logger.info(
            "Schema %s: %d file(s) parsed, %d with parse gaps%s",
            schema_name,
            done,
            len(gaps),
            f", {len(timed_out)} timed out" if timed_out else "",
        )
        for rel, line, sample in gaps[:20]:
            logger.warning("  parse gap: %s:%d  %s", rel, line, sample)
        if len(gaps) > 20:
            logger.warning("  ... and %d more file(s) with parse gaps", len(gaps) - 20)
        for rel in timed_out[:20]:
            logger.warning("  timed out (skipped): %s", rel)
        return raw_files

    def _parse_strict(self, files: list[Path], schema_name: str, root: Path) -> list[RawPlsqlFile]:
        raw_files: list[RawPlsqlFile] = []
        for index, path in enumerate(files, start=1):
            rel = str(path.relative_to(root))
            text, encoding = _decode(path.read_bytes(), self._config.encodings)
            try:
                raw = _sa().parse_declarations(text, rel, strict=True)
            except SqlSyntaxError as error:
                raise self._halt(rel, text, encoding, error, files_parsed_ok=index - 1) from error
            raw.path = rel
            raw_files.append(raw)
            if index % _PROGRESS_EVERY == 0:
                logger.info("Schema %s: parsed %d/%d files", schema_name, index, len(files))
        logger.info("Schema %s: %d file(s) parsed (strict, all clean)", schema_name, len(files))
        return raw_files

    def _halt(
        self, rel: str, text: str, encoding: str, error: SqlSyntaxError, *, files_parsed_ok: int
    ) -> StrictHaltError:
        """Build the strict-mode halt: write a full diagnostic report, return the error to
        raise (nothing has been yielded yet, so nothing is in the graph)."""
        progress = Progress()
        progress.files_parsed_ok = files_parsed_ok
        report = build_failure_report(
            file_path=rel, source=text, encoding=encoding, error=error, progress=progress
        )
        diagnostics_dir = self._diagnostics_dir or "auryn-diagnostics"
        report_path = write_report(diagnostics_dir, report, files_parsed_ok)
        logger.error("STRICT halt on %s at line %d col %d: %s", rel, error.line, error.column, error.message)
        return StrictHaltError(
            f"{rel} failed to parse at line {error.line}, column {error.column}: {error.message}",
            report,
            report_path,
        )


def _decode(raw: bytes, encodings: tuple[str, ...]) -> tuple[str, str]:
    """Decode `raw` by trying `encodings` in order; return (text, encoding_used). Object DDL
    is normally UTF-8/ASCII, so the first attempt wins; the fallbacks cover a legacy export.
    A last-resort lenient UTF-8 decode never fails, so parsing always gets *some* text."""
    for encoding in encodings:
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8-replace"


def _discover_schemas(root: Path) -> list[Path]:
    """Schema folders under `root`: any subdirectory carrying a SchemaInformation.xml or a
    known object folder. Falls back to treating `root` itself as one schema (a flat export
    with no per-schema nesting)."""
    schemas = [child for child in sorted(root.iterdir()) if child.is_dir() and _looks_like_schema(child)]
    if not schemas and _looks_like_schema(root):
        return [root]
    return schemas


def _looks_like_schema(directory: Path) -> bool:
    if (directory / "SchemaInformation.xml").exists():
        return True
    return any(child.is_dir() and child.name.lower() in _OBJECT_FOLDERS for child in directory.iterdir())
