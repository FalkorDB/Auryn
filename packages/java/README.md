# auryn-java

Java language plugin for Auryn: a fast tree-sitter-java engine extracting packages/classes/interfaces/enums/records/methods/fields/params, the type hierarchy (EXTENDS/IMPLEMENTS), and calls. A deep pass (Eclipse JDT / javac symbol resolution) is the planned second phase.

Part of [Auryn](https://github.com/FalkorDB/Auryn), which turns a codebase into a queryable knowledge graph for AI agents.

## Install

```bash
pip install falkordb-auryn                  # Auryn with every plugin
pip install "auryn-core[java]"              # Auryn with only this plugin
```

## License

MIT. See the [LICENSE](https://github.com/FalkorDB/Auryn/blob/main/packages/java/LICENSE).
