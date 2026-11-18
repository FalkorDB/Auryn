# auryn-http

Language-agnostic HTTP call<->endpoint stitch for Auryn (ADR 0006): a post-ingestion hook that resolves route-keyed APIEndpoint placeholders (emitted by any caller language) to their handler Methods across the shared multi-repo graph, by route and HTTP verb (MVC `/Controller/Action` routes match by name only). Order-independent, 1-to-many, provenance-scoped. Depends only on auryn-core.

Part of [Auryn](https://github.com/FalkorDB/Auryn), which turns a codebase into a queryable knowledge graph for AI agents.

## Install

```bash
pip install falkordb-auryn                  # Auryn with every plugin
pip install "auryn-core[http]"              # Auryn with only this plugin
```

## License

MIT. See the [LICENSE](https://github.com/FalkorDB/Auryn/blob/main/packages/http/LICENSE).
