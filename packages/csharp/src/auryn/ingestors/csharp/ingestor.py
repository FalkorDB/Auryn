"""Extracts Namespace/File/Class/Interface/Struct/Enum/Record/Delegate/DTO/Method/
Property/Field/Event/Parameter/EnumMember/APIEndpoint structure from a directory of
C# source files.

Phase 1 (parsing every file) runs in parallel across a ProcessPoolExecutor -- see the
C# ingestor plan for why a process pool rather than threads (the GIL means the
AST-walking cost in file_parser.py wouldn't parallelize under threads anyway). Phase
2 (everything needing cross-file information: EXTENDS/IMPLEMENTS, DTO/API endpoint
classification, call/type-reference resolution) then runs sequentially, in
resolution.py.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path

import click

from auryn.core.base_ingestor import BaseIngestor
from auryn.core.graph_client import GraphWriter, connect
from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.core.parallel import make_process_pool, resolve_workers
from auryn.core.registry import register_ingestor
from auryn.ingestors.csharp import resolution
from auryn.ingestors.csharp.config import CSharpSourceConfig, project_id
from auryn.ingestors.csharp.file_parser import parse_file
from auryn.ingestors.csharp.raw_model import RawFile
from auryn.ingestors.csharp.schema import EDGE_SCHEMAS, NODE_SCHEMAS

logger = logging.getLogger(__name__)

_EXCLUDED_DIR_NAMES = {"bin", "obj"}


@register_ingestor("csharp")
class CSharpIngestor(BaseIngestor):
    node_schemas = NODE_SCHEMAS
    edge_schemas = EDGE_SCHEMAS

    def __init__(self, config: CSharpSourceConfig) -> None:
        self._config = config

    @classmethod
    def build_cli_command(cls) -> click.Command:
        @click.command(
            "csharp",
            help="Ingest a C# codebase (classes, interfaces, methods, API endpoints, DTOs, ...).",
        )
        @click.option(
            "--path",
            required=True,
            type=click.Path(exists=True, file_okay=False),
            help="Root directory to scan for .cs files (recursively).",
        )
        @click.option(
            "--project-name", required=True, help="Logical name for this project, used to build the Project node's id."
        )
        @click.option("--solution-name", default=None, help="Optional: wraps the Project in a Solution node.")
        @click.option("--target-framework", default=None, help="Free-text, e.g. net8.0 -- informational only.")
        @click.option(
            "--dto-suffix",
            "dto_suffixes",
            multiple=True,
            help="DTO name-suffix heuristic (repeatable). Default: Dto, Request, Response, ViewModel.",
        )
        @click.option(
            "--workers", default=None, type=int, help="Process pool size for parallel per-file parsing. Default: os.cpu_count()."
        )
        @click.pass_context
        def command(
            ctx: click.Context,
            path: str,
            project_name: str,
            solution_name: str | None,
            target_framework: str | None,
            dto_suffixes: tuple[str, ...],
            workers: int | None,
        ) -> None:
            writer = GraphWriter(connect(ctx.obj["falkordb_config"]))
            config = CSharpSourceConfig(
                path=path,
                project_name=project_name,
                solution_name=solution_name,
                target_framework=target_framework,
                dto_name_suffixes=tuple(dto_suffixes) or CSharpSourceConfig.dto_name_suffixes,
                workers=workers,
            )
            stats = cls(config).run(writer)
            click.echo(
                f"Merged {stats.nodes_merged} nodes, {stats.edges_merged} edges "
                f"in {stats.elapsed_seconds:.2f}s"
            )

        return command

    def extract(self) -> Iterator[GraphNode | GraphEdge]:
        root = Path(self._config.path).resolve()
        files = self._discover_files(root)
        logger.info("Found %d .cs files under %s", len(files), root)

        raw_files = self._parse_files(files, root)

        proj_id = project_id(self._config.project_name, self._config.solution_name)
        yield GraphNode(
            "Project",
            {"id": proj_id},
            {"name": self._config.project_name, "target_framework": self._config.target_framework},
        )
        if self._config.solution_name:
            # The Project node must already be merged before this edge -- MERGE's
            # MATCH silently no-ops (with a logged warning) if either endpoint is
            # missing, same ordering lesson as the SQL ingestor's FK pass.
            solution_id = f"csharp://{self._config.solution_name}"
            yield GraphNode("Solution", {"id": solution_id}, {"name": self._config.solution_name})
            yield GraphEdge("HAS_PROJECT", "Solution", {"id": solution_id}, "Project", {"id": proj_id})

        yield from resolution.resolve(raw_files, proj_id, self._config.dto_name_suffixes)

    def _discover_files(self, root: Path) -> list[Path]:
        return sorted(
            p
            for p in root.rglob("*.cs")
            if not _EXCLUDED_DIR_NAMES & set(p.relative_to(root).parts[:-1])
        )

    def _parse_files(self, files: list[Path], root: Path) -> list[RawFile]:
        workers = resolve_workers(self._config.workers)
        paths = [str(f) for f in files]
        if workers == 1 or len(paths) <= 1:
            raw_files = [parse_file(p) for p in paths]
        else:
            # Preload the parse module (parse_file's home) into the forkserver control process so
            # workers inherit its tree-sitter import via COW rather than re-importing -- see
            # core.parallel.
            with make_process_pool(workers, preload=["auryn.ingestors.csharp.file_parser"]) as pool:
                raw_files = list(pool.map(parse_file, paths))
        for raw_file in raw_files:
            raw_file.path = Path(raw_file.path).relative_to(root).as_posix()
        return raw_files
