# auryn-ruby

Ruby language plugin for Auryn: a fast tree-sitter-ruby engine extracting modules/classes/methods/functions/parameters/fields/constants, the class hierarchy (EXTENDS/INCLUDES), require graph (IMPORTS), and calls. A deep pass (a real Ruby loader / Sorbet / RBS symbol resolver for method dispatch and receiver types) is the planned second phase -- Ruby is dynamic, so the fast pass is name-based only.

Part of [Auryn](https://github.com/FalkorDB/Auryn), which turns a codebase into a queryable knowledge graph for AI agents.

## Install

```bash
pip install auryn-ruby
```

To install Auryn with every language plugin, use `pip install falkordb-auryn`.

## License

SSPL-1.0. See the [LICENSE](https://github.com/FalkorDB/Auryn/blob/main/LICENSE).
