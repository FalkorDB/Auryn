# auryn-c

C language plugin for Auryn: fast tree-sitter-c engine (functions/structs/unions/enums/typedefs/globals/macros/#includes/calls, no build config). A deep libclang engine (resolved types, header<->.c linking, cross-TU calls) is the planned second phase.

Part of [Auryn](https://github.com/FalkorDB/Auryn), which turns a codebase into a queryable knowledge graph for AI agents.

## Install

```bash
pip install falkordb-auryn                  # Auryn with every plugin
pip install "auryn-core[c]"                 # Auryn with only this plugin
```

## License

MIT. See the [LICENSE](https://github.com/FalkorDB/Auryn/blob/main/packages/c/LICENSE).
