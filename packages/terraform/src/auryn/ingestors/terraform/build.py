"""Turn parsed Terraform into the deployment-tier graph (ADR 0007): File -> Resource; resources
DEPENDS_ON the resources they reference (within the same MODULE -- a directory), a container image in
a resource RUNS_IMAGE the shared `Image` hub, and an rds/ecr RESOURCE (not a data-source lookup)
PROVISIONS a `Database`/`Registry`.

Emits no cross-artifact edge beyond the shared `Image` hub; the `deploy-terraform` enrichment runs the
image lifecycle."""

from __future__ import annotations

import posixpath
from collections.abc import Iterator

from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.core.image_ref import image_id, image_node
from auryn.ingestors.terraform import ids
from auryn.ingestors.terraform.model import ParsedTf

_DB_TYPES = frozenset({
    "aws_db_instance", "aws_rds_cluster", "aws_rds_cluster_instance", "aws_dynamodb_table",
    "google_sql_database_instance", "azurerm_postgresql_server", "azurerm_postgresql_flexible_server",
    "azurerm_mysql_server", "azurerm_mysql_flexible_server", "azurerm_cosmosdb_account",
})
_REGISTRY_TYPES = frozenset({
    "aws_ecr_repository", "google_artifact_registry_repository", "azurerm_container_registry",
})


def _category(b) -> str:
    if b.block_type != "resource":
        return b.block_type          # "data" | "module"
    if b.rtype in _DB_TYPES:
        return "database"
    if b.rtype in _REGISTRY_TYPES:
        return "registry"
    return "compute"


def build_graph(files: list[tuple[str, ParsedTf]], proj_id: str) -> Iterator[GraphNode | GraphEdge]:
    # Per-MODULE (directory) index: a `${aws_x.y...}` reference resolves to a block in the SAME module,
    # and same-named resources in different modules (`env/dev` vs `env/prod`) stay distinct.
    by_module: dict[str, dict[str, str]] = {}
    for rel, parsed in files:
        md = posixpath.dirname(rel)
        idx = by_module.setdefault(md, {})
        for b in parsed.blocks:
            idx[b.address] = ids.resource_id(proj_id, md, b.address)

    for rel_path, parsed in files:
        md = posixpath.dirname(rel_path)
        idx = by_module.get(md, {})
        fid = ids.file_id(proj_id, rel_path)
        yield GraphNode("File", {"id": fid}, {"path": rel_path, "name": posixpath.basename(rel_path)})
        yield GraphEdge("HAS_FILE", "Project", {"id": proj_id}, "File", {"id": fid})

        for b in parsed.blocks:
            rid = ids.resource_id(proj_id, md, b.address)
            yield GraphNode("Resource", {"id": rid}, {
                "block_type": b.block_type, "rtype": b.rtype, "name": b.name,
                "address": b.address, "module": md, "category": _category(b),
            })
            yield GraphEdge("HAS_RESOURCE", "File", {"id": fid}, "Resource", {"id": rid})

            for ref in b.images:
                yield image_node(ref)
                yield GraphEdge("RUNS_IMAGE", "Resource", {"id": rid}, "Image", {"id": image_id(ref)})

            # A `data` source is a READ of existing infra -- it does not PROVISION anything.
            if b.block_type == "resource":
                if b.rtype in _DB_TYPES:
                    did = ids.database_id(proj_id, md, b.address)
                    yield GraphNode("Database", {"id": did}, {"name": b.name, "rtype": b.rtype})
                    yield GraphEdge("PROVISIONS", "Resource", {"id": rid}, "Database", {"id": did})
                elif b.rtype in _REGISTRY_TYPES:
                    reg = ids.registry_id(proj_id, md, b.address)
                    yield GraphNode("Registry", {"id": reg}, {"name": b.name, "rtype": b.rtype})
                    yield GraphEdge("PROVISIONS", "Resource", {"id": rid}, "Registry", {"id": reg})

        for b in parsed.blocks:
            rid = ids.resource_id(proj_id, md, b.address)
            for ref in b.refs:
                target = idx.get(ref)  # same-module resolution
                if target and target != rid:
                    yield GraphEdge("DEPENDS_ON", "Resource", {"id": rid}, "Resource", {"id": target})
