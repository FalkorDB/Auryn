# auryn-docker

Dockerfile plugin for Auryn and the first producer of the deployment & infrastructure tier (ADR 0007). A dependency-free Dockerfile parser emits BuildStage nodes, the shared image-keyed `Image` hub (the FROM chain, as resolved:false placeholders), and multi-stage COPY --from edges; a companion `deploy` enrichment hook stitches build stages to the code they package (PACKAGES), recomputes image `resolved`, and GCs orphan images. The hub that Compose/k8s/Helm/Terraform producers join onto. Depends only on auryn-core.

Part of [Auryn](https://github.com/FalkorDB/Auryn), which turns a codebase into a queryable knowledge graph for AI agents.

## Install

```bash
pip install falkordb-auryn                  # Auryn with every plugin
pip install "auryn-core[docker]"            # Auryn with only this plugin
```

## License

MIT. See the [LICENSE](https://github.com/FalkorDB/Auryn/blob/main/packages/docker/LICENSE).
