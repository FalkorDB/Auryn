# auryn-terraform

Terraform/OpenTofu plugin for Auryn and the cloud-topology producer of the deployment & infrastructure tier (ADR 0007). Parses HCL (`.tf`) into Resource nodes (resource/module/data) and DEPENDS_ON edges from interpolation references, plus the cross-tier seams: a container image in a resource RUNS_IMAGE the shared Image hub; `aws_db_instance`/rds → Database and `aws_ecr_repository` → Registry (PROVISIONS). A `deploy-terraform` enrichment runs the shared image lifecycle. Composes with auryn-docker/auryn-compose/auryn-k8s onto the same hub. Depends on auryn-core and python-hcl2.

Part of [Auryn](https://github.com/FalkorDB/Auryn), which turns a codebase into a queryable knowledge graph for AI agents.

## Install

```bash
pip install falkordb-auryn                  # Auryn with every plugin
pip install "auryn-core[terraform]"         # Auryn with only this plugin
```

## License

MIT. See the [LICENSE](https://github.com/FalkorDB/Auryn/blob/main/packages/terraform/LICENSE).
