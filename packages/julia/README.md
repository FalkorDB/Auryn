# auryn-julia

Julia language plugin for Auryn: a fast tree-sitter-julia engine extracting modules/structs/abstract+primitive types/generic functions/methods/fields/params, the subtype hierarchy (EXTENDS), multiple-dispatch (DISPATCHES_ON), and calls. A deep pass (Julia's own type inference for precise method resolution) is the planned second phase.

Part of [Auryn](https://github.com/FalkorDB/Auryn), which turns a codebase into a queryable knowledge graph for AI agents.

## Install

```bash
pip install auryn-julia
```

To install Auryn with every language plugin, use `pip install falkordb-auryn`.

## License

SSPL-1.0. See the [LICENSE](https://github.com/FalkorDB/Auryn/blob/main/LICENSE).
