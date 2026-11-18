# auryn-cuda

CUDA language plugin for Auryn: a fast tree-sitter-cuda engine. CUDA is a superset of C++, so it reuses the auryn-cpp extraction engine verbatim (namespaces/classes/structs/enums/methods/fields/params, type hierarchy, templates, includes/macros, calls) and adds the CUDA-only graph signals: an `execution_space` property (`__global__`/`__device__`/`__host__`) and a LAUNCHES edge for `kernel<<<...>>>` launches. A deep libclang/nvcc pass is the planned second phase.

Part of [Auryn](https://github.com/FalkorDB/Auryn), which turns a codebase into a queryable knowledge graph for AI agents.

## Install

```bash
pip install falkordb-auryn                  # Auryn with every plugin
pip install "auryn-core[cuda]"              # Auryn with this plugin and auryn-cpp, which it reuses
```

## License

MIT. See the [LICENSE](https://github.com/FalkorDB/Auryn/blob/main/packages/cuda/LICENSE).
