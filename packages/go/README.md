# auryn-go

Go language plugin for Auryn: a fast tree-sitter-go engine extracting packages/structs/interfaces/functions/methods/fields/params/consts, Go embedding (EMBEDS), and calls, plus the HTTP seam: outbound net/http requests and the route enrichment (net/http, gin, echo, chi, gorilla/mux). IMPLEMENTS (structural interface satisfaction) and precise cross-file type resolution are the planned deep pass (a go/types or gopls-backed engine).

Part of [Auryn](https://github.com/FalkorDB/Auryn), which turns a codebase into a queryable knowledge graph for AI agents.

## Install

```bash
pip install falkordb-auryn                  # Auryn with every plugin
pip install "auryn-core[go]"                # Auryn with only this plugin
```

## License

MIT. See the [LICENSE](https://github.com/FalkorDB/Auryn/blob/main/packages/go/LICENSE).
