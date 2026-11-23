# auryn-kustomize

Kustomize plugin for Auryn (deployment & infrastructure tier, ADR 0007). Render-free: parses `kustomization.yaml` into a Resource (the overlay/base) that RUNS_IMAGE the shared Image hub for each ref its `images:` transformer sets (`newName`/`newTag`/`digest` → a concrete ref), and DEPENDS_ON the base kustomizations it layers on (`resources:`/`bases:`/`components:` that resolve to another in-repo kustomization). A `deploy-kustomize` enrichment runs the shared image lifecycle. Composes with auryn-docker/auryn-compose/auryn-k8s/auryn-terraform/auryn-helm onto the same hub. Depends on auryn-core and PyYAML.

v1 is static and render-free: `kustomize build` (rendering the patched base manifests) is a follow-up; v1 reads only the `kustomization.yaml` declarations.

Part of [Auryn](https://github.com/FalkorDB/Auryn), which turns a codebase into a queryable knowledge graph for AI agents.

## Install

```bash
pip install falkordb-auryn                  # Auryn with every plugin
pip install "auryn-core[kustomize]"         # Auryn with only this plugin
```

## License

MIT. See the [LICENSE](https://github.com/FalkorDB/Auryn/blob/main/packages/kustomize/LICENSE).
