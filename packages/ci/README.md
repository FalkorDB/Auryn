# auryn-ci

CI plugin for Auryn (deployment & infrastructure tier, ADR 0007). Parses **GitHub Actions** workflows (`.github/workflows/*.yml`) into a Resource (the pipeline) that **BUILDS_IMAGE** the shared Image hub for each image its steps build or push — a `docker/build-push-action` step's `tags`, or a `docker build -t` / `docker push` in a `run:` step. That `BUILDS_IMAGE` edge flips a hub image `resolved`/`internal`, **closing the build→image→run loop** the docker producer deliberately defers to CI (a Dockerfile doesn't name the image it produces). A `deploy-ci` enrichment runs the shared image lifecycle. Composes with docker/compose/k8s/terraform/helm/kustomize onto the same hub. Depends on auryn-core and PyYAML.

v1 is GitHub Actions and static (no execution). Other CI systems (GitLab CI, Jenkins) and `Registry`/`DEPLOYS` edges are follow-ups.

Part of [Auryn](https://github.com/FalkorDB/Auryn), which turns a codebase into a queryable knowledge graph for AI agents.

## Install

```bash
pip install falkordb-auryn                  # Auryn with every plugin
pip install "auryn-core[ci]"                # Auryn with only this plugin
```

## License

MIT. See the [LICENSE](https://github.com/FalkorDB/Auryn/blob/main/packages/ci/LICENSE).
