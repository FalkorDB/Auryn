# auryn-k8s

Kubernetes-manifest plugin for Auryn and the third producer of the deployment & infrastructure tier (ADR 0007). Parses k8s manifests into Service (workload) nodes that RUNS_IMAGE the shared Image hub, and Ingress route nodes; a `deploy-k8s` enrichment joins each Ingress route to the HTTP seam's route-keyed APIEndpoint (SERVES) -- so an external URL reaches the handler that serves it -- and runs the shared image lifecycle. Composes with auryn-docker/auryn-compose onto the same hub. Depends only on auryn-core.

Part of [Auryn](https://github.com/FalkorDB/Auryn), which turns a codebase into a queryable knowledge graph for AI agents.

## Install

```bash
pip install falkordb-auryn                  # Auryn with every plugin
pip install "auryn-core[k8s]"               # Auryn with only this plugin
```

## License

MIT. See the [LICENSE](https://github.com/FalkorDB/Auryn/blob/main/packages/k8s/LICENSE).
