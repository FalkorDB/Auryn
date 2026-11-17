# auryn-core

Core of Auryn: registry/plugin discovery, git+local sources, the FalkorDB graph layer, the MCP server, the viewer, and the `auryn` CLI. Language support ships as separate `auryn-<lang>` plugins.

Part of [Auryn](https://github.com/FalkorDB/Auryn), which turns a codebase into a queryable knowledge graph for AI agents.

## Install

```bash
pip install falkordb-auryn                  # Auryn with every plugin
pip install "auryn-core[python,http]"       # the core with only the plugins you name
```

Plugins are named after their packages without `auryn-`; see [What it can read](https://github.com/FalkorDB/Auryn#what-it-can-read).

## License

MIT. See the [LICENSE](https://github.com/FalkorDB/Auryn/blob/main/packages/core/LICENSE).
