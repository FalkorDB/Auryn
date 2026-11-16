# auryn-cpp

C++ language plugin for Auryn: a fast tree-sitter-cpp engine extracting namespaces/classes/structs/unions/enums (incl. `enum class`)/methods/fields/params/free functions, the class hierarchy (EXTENDS, multiple inheritance), templates, #includes/macros, and calls -- including out-of-line `Class::method` definitions resolved back to their class. A deep libclang engine (template instantiation, overload resolution, ADL, precise header linking) is the planned second phase.

Part of [Auryn](https://github.com/FalkorDB/Auryn), which turns a codebase into a queryable knowledge graph for AI agents.

## Install

```bash
pip install auryn-cpp
```

To install Auryn with every language plugin, use `pip install falkordb-auryn`.

## License

SSPL-1.0. See the [LICENSE](https://github.com/FalkorDB/Auryn/blob/main/LICENSE).
