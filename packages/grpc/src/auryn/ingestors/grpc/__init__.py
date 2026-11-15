"""The gRPC/Protobuf contract stitch -- a registered graph enrichment (post-pass), not a BaseIngestor
or a CLI command. It runs after the language ingestors (via core.ingest.run_post_hooks, on both the
full and incremental paths), parses the repo's `.proto` service contracts, and links a client's RPC
call and a server's handler Method to the shared contract across languages. See stitch.py."""

from auryn.core.registry import GraphEnrichment, register_graph_enrichment
from auryn.ingestors.grpc.stitch import run as _run

register_graph_enrichment(GraphEnrichment(name="grpc-contract", run=_run))

__all__ = ["_run"]
