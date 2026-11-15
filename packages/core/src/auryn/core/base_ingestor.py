"""Base class every ingestor implements, plus the run-loop shared by all of them."""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from typing import ClassVar

import click

from auryn.core.graph_client import GraphWriter
from auryn.core.graph_model import EdgeSchema, GraphEdge, GraphNode, NodeSchema

_BATCH_SIZE = 2000
"""Items buffered from extract() before flushing as one batched write -- see
graph_client.py's _WRITE_BATCH_SIZE for why batching beats one query per item."""


@dataclass
class IngestionStats:
    nodes_merged: int = 0
    edges_merged: int = 0
    elapsed_seconds: float = 0.0


class BaseIngestor(ABC):
    """Extension point for a new source type.

    Subclass this, decorate with @register_ingestor("<name>"), declare
    node_schemas/edge_schemas, and implement extract(). See CONTRIBUTING.md.
    """

    name: ClassVar[str]
    node_schemas: ClassVar[list[NodeSchema]] = []
    edge_schemas: ClassVar[list[EdgeSchema]] = []

    @abstractmethod
    def extract(self) -> Iterator[GraphNode | GraphEdge]:
        """Yield every node/edge discovered from the source, in any order."""

    @classmethod
    def build_cli_command(cls) -> click.Command:
        """Optional: return a click.Command exposing this ingestor's config as CLI
        options. Overriding this is what makes `auryn run <name>` work -- the CLI
        discovers and attaches it automatically, no changes to cli.py needed.
        Ingestors that skip this can still be used programmatically."""
        raise NotImplementedError(f"{cls.__name__} does not provide a CLI command")

    def run(self, writer: GraphWriter) -> IngestionStats:
        logger = logging.getLogger(f"auryn.{self.name}")
        started = time.monotonic()

        logger.info("Starting ingestion")
        writer.setup_schema(self.node_schemas)

        stats = IngestionStats()
        node_batch: list[GraphNode] = []
        edge_batch: list[GraphEdge] = []

        def flush() -> None:
            # Nodes always flush before edges, batch by batch -- same ordering
            # guarantee as merging one item at a time, just at _BATCH_SIZE
            # granularity, so an edge never gets merged before the node it
            # references if that node was buffered in this same batch.
            if node_batch:
                writer.merge_nodes(node_batch)
                stats.nodes_merged += len(node_batch)
                node_batch.clear()
            if edge_batch:
                writer.merge_edges(edge_batch)
                stats.edges_merged += len(edge_batch)
                edge_batch.clear()

        for item in self.extract():
            if isinstance(item, GraphNode):
                node_batch.append(item)
            else:
                edge_batch.append(item)
            if len(node_batch) + len(edge_batch) >= _BATCH_SIZE:
                flush()
        flush()

        stats.elapsed_seconds = time.monotonic() - started
        logger.info(
            "Finished ingestion: %d nodes, %d edges merged in %.2fs",
            stats.nodes_merged,
            stats.edges_merged,
            stats.elapsed_seconds,
        )
        return stats
