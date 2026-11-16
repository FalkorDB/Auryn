# auryn-php

PHP language plugin for Auryn: a fast tree-sitter-php engine extracting namespaces/classes/interfaces/traits/enums/functions/methods/properties/constants/params, the type hierarchy (EXTENDS/IMPLEMENTS/USES_TRAIT), and calls. A deep pass (full name-resolution: use-statement aliasing, autoloading, inherited-member calls) is the planned second phase.

Part of [Auryn](https://github.com/FalkorDB/Auryn), which turns a codebase into a queryable knowledge graph for AI agents.

## Install

```bash
pip install auryn-php
```

To install Auryn with every language plugin, use `pip install falkordb-auryn`.

## License

SSPL-1.0. See the [LICENSE](https://github.com/FalkorDB/Auryn/blob/main/LICENSE).
