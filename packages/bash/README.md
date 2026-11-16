# auryn-bash

Bash/Shell language plugin for Auryn: a fast tree-sitter-bash engine extracting the functions a script defines, the local-functions and external commands each invokes (CALLS), and `source`/`.` includes (IMPORTS). Bash has no types/classes, so the model is deliberately flat (File -> Function -> calls). A deep pass (a real shell resolver / shellcheck-style analysis) is a possible future second phase.

Part of [Auryn](https://github.com/FalkorDB/Auryn), which turns a codebase into a queryable knowledge graph for AI agents.

## Install

```bash
pip install auryn-bash
```

To install Auryn with every language plugin, use `pip install falkordb-auryn`.

## License

SSPL-1.0. See the [LICENSE](https://github.com/FalkorDB/Auryn/blob/main/LICENSE).
