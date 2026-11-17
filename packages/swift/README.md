# auryn-swift

Swift language plugin for Auryn: a fast tree-sitter-swift engine extracting classes/structs/enums/protocols/functions/methods/initializers/properties/params/cases, the type hierarchy (EXTENDS/IMPLEMENTS from superclass + protocol conformance, including via extensions), imports, and calls. A deep pass (SourceKit-LSP symbol resolution) is the planned second phase.

Part of [Auryn](https://github.com/FalkorDB/Auryn), which turns a codebase into a queryable knowledge graph for AI agents.

## Install

```bash
pip install falkordb-auryn                  # Auryn with every plugin
pip install "auryn-core[swift]"             # Auryn with only this plugin
```

## License

MIT. See the [LICENSE](https://github.com/FalkorDB/Auryn/blob/main/packages/swift/LICENSE).
