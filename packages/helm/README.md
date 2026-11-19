# auryn-helm

Helm-chart plugin for Auryn (deployment & infrastructure tier, ADR 0007). Render-free static parse: a chart's `Chart.yaml` + `values.yaml` become a Resource (the chart/release) that DEPENDS_ON its chart dependencies (resolved to an in-repo chart by name, else a placeholder), RUNS_IMAGE the shared Image hub for each image declared in `values.yaml`, and MOUNTS the chart's notable `ConfigValue`s. A `deploy-helm` enrichment runs the shared image lifecycle. Composes with auryn-docker/auryn-compose/auryn-k8s/auryn-terraform onto the same hub. Depends on auryn-core and PyYAML.

v1 is static and render-free: a chart's `templates/*.yaml` are Go-templated (not valid YAML) and are not parsed — resolving the workloads they declare needs a `helm template` render, a future k8s-producing arm.

Part of [Auryn](https://github.com/FalkorDB/Auryn), which turns a codebase into a queryable knowledge graph for AI agents.

## Install

```bash
pip install falkordb-auryn                  # Auryn with every plugin
pip install "auryn-core[helm]"              # Auryn with only this plugin
```

## License

MIT. See the [LICENSE](https://github.com/FalkorDB/Auryn/blob/main/packages/helm/LICENSE).
