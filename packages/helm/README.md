# auryn-helm

Helm-chart plugin for Auryn (deployment & infrastructure tier, ADR 0007). Render-free: parses `Chart.yaml`/`values.yaml`/`templates/` into a Resource (the chart/release) that DEPENDS_ON its chart dependencies and DEPLOYS the Service workloads its templates declare; a workload RUNS_IMAGE the shared Image hub for each image it can fully resolve from `values.yaml` (`{{ .Values.image.repository }}:{{ .Values.image.tag }}` → a concrete ref; unresolved refs are skipped). Composes with auryn-docker/auryn-compose/auryn-k8s/auryn-terraform onto the same hub. Depends on auryn-core and PyYAML.

Part of [Auryn](https://github.com/FalkorDB/Auryn), which turns a codebase into a queryable knowledge graph for AI agents.

## Install

```bash
pip install falkordb-auryn                  # Auryn with every plugin
pip install "auryn-core[helm]"              # Auryn with only this plugin
```

## License

MIT. See the [LICENSE](https://github.com/FalkorDB/Auryn/blob/main/packages/helm/LICENSE).
