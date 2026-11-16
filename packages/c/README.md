# auryn-c

C language plugin for Auryn: fast tree-sitter-c engine (functions/structs/unions/enums/typedefs/globals/macros/#includes/calls, no build config). A deep libclang engine (resolved types, header<->.c linking, cross-TU calls) is the planned second phase.

Part of [Auryn](https://github.com/FalkorDB/Auryn), which turns a codebase into a queryable knowledge graph for AI agents.

## Install

```bash
pip install auryn-c
```

To install Auryn with every language plugin, use `pip install falkordb-auryn`.

## License

SSPL-1.0. See the [LICENSE](https://github.com/FalkorDB/Auryn/blob/main/LICENSE).
