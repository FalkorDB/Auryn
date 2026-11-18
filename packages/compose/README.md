# auryn-compose

Docker Compose plugin for Auryn and the second producer of the deployment & infrastructure tier (ADR 0007). Parses docker-compose files into Service nodes (RUNS_IMAGE the shared Image hub, DEPENDS_ON other services), and a `deploy-compose` enrichment hook that closes the build->run loop: it resolves each service's `build:` to the Dockerfile's final BuildStage and links BUILDS_IMAGE, flipping that image to resolved/internal. Composes with auryn-docker onto the same image-keyed hub. Depends only on auryn-core.

Part of [Auryn](https://github.com/FalkorDB/Auryn), which turns a codebase into a queryable knowledge graph for AI agents.

## Install

```bash
pip install falkordb-auryn                  # Auryn with every plugin
pip install "auryn-core[compose]"           # Auryn with only this plugin
```

## License

MIT. See the [LICENSE](https://github.com/FalkorDB/Auryn/blob/main/packages/compose/LICENSE).
