# auryn-rust

Rust language plugin for Auryn: a fast tree-sitter-rust engine extracting modules/structs/enums/traits/functions/methods/fields/variants/params/consts, the trait hierarchy (IMPLEMENTS/EXTENDS), and calls. A deep pass (rust-analyzer / rustc HIR symbol resolution) is the planned second phase.

Part of [Auryn](https://github.com/FalkorDB/Auryn), which turns a codebase into a queryable knowledge graph for AI agents.

## Install

```bash
pip install falkordb-auryn                  # Auryn with every plugin
pip install "auryn-core[rust]"              # Auryn with only this plugin
```

## License

MIT. See the [LICENSE](https://github.com/FalkorDB/Auryn/blob/main/packages/rust/LICENSE).
