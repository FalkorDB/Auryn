# auryn-compose

Docker Compose plugin for Auryn (deployment & infrastructure tier, ADR 0007). Parses docker-compose files into Service nodes (RUNS_IMAGE the shared Image hub, DEPENDS_ON other services). A `deploy-compose` enrichment links each service that has both `build:` and `image:` to the Dockerfile stage it builds (`build.target`, else the final stage) with BUILDS_IMAGE, marking the image resolved/internal; the stages come from auryn-docker, so install both. Depends on auryn-core and PyYAML.

Part of [Auryn](https://github.com/FalkorDB/Auryn), which turns a codebase into a queryable knowledge graph for AI agents.

## Install

```bash
pip install falkordb-auryn                  # Auryn with every plugin
pip install "auryn-core[compose]"           # Auryn with only this plugin
pip install "auryn-core[compose,docker]"    # + auryn-docker, for the build -> image links
```

## License

MIT. See the [LICENSE](https://github.com/FalkorDB/Auryn/blob/main/packages/compose/LICENSE).
