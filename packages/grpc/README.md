# auryn-grpc

Language-agnostic gRPC/Protobuf contract stitch for Auryn: a post-ingestion hook that parses .proto service contracts and links a client's RPC call and a server's handler Method to the shared contract across languages (caller -CALLS_RPC-> RpcMethod <-IMPLEMENTS_RPC- handler). The RPC analog of the HTTP call<->endpoint seam; ties microservices written in different languages. Depends only on auryn-core.

Part of [Auryn](https://github.com/FalkorDB/Auryn), which turns a codebase into a queryable knowledge graph for AI agents.

## Install

```bash
pip install auryn-grpc
```

To install Auryn with every language plugin, use `pip install falkordb-auryn`.

## License

SSPL-1.0. See the [LICENSE](https://github.com/FalkorDB/Auryn/blob/main/LICENSE).
