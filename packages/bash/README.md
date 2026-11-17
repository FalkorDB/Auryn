# auryn-bash

Bash/Shell language plugin for Auryn: a fast tree-sitter-bash engine extracting the functions a script defines, the local-functions and external commands each invokes (CALLS), and `source`/`.` includes (IMPORTS). Bash has no types/classes, so the model is deliberately flat (File -> Function -> calls). A deep pass (a real shell resolver / shellcheck-style analysis) is a possible future second phase.

Part of [Auryn](https://github.com/FalkorDB/Auryn), which turns a codebase into a queryable knowledge graph for AI agents.

## Install

```bash
pip install falkordb-auryn                  # Auryn with every plugin
pip install "auryn-core[bash]"              # Auryn with only this plugin
```

## License

MIT. See the [LICENSE](https://github.com/FalkorDB/Auryn/blob/main/packages/bash/LICENSE).
