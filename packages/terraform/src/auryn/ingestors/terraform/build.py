"""Turn parsed Terraform into the deployment-tier graph (ADR 0007): File -> Resource; resources
DEPENDS_ON the resources they reference (the Terraform dependency graph); a container image in a
resource RUNS_IMAGE the shared `Image` hub (so a tf-defined ECS/Lambda running an image joins the hub
docker builds / compose+k8s run); an rds/ecr resource PROVISIONS a `Database`/`Registry`.

Emits no cross-artifact edge beyond the shared `Image` hub; the `deploy-terraform` enrichment runs the
image lifecycle."""

from __future__ import annotations

import posixpath
from collections.abc import Iterator

from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.core.image_ref import image_id, parse_image_ref
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
    if b.rtype in _DB_TYPES:
        return "database"
    if b.rtype in _REGISTRY_TYPES:
        return "registry"
    return b.block_type  # resource | module | data


def build_graph(files: list[tuple[str, ParsedTf]], proj_id: str) -> Iterator[GraphNode | GraphEdge]:
    # Index every block address across ALL files, so a `${module.x...}` / `${aws_y.z...}` reference in
    # one file resolves to a Resource declared in another.
    addr_to_id = {b.address: ids.resource_id(proj_id, b.address) for _rel, parsed in files for b in parsed.blocks}

    for rel_path, parsed in files:
        fid = ids.file_id(proj_id, rel_path)
        yield GraphNode("File", {"id": fid}, {"path": rel_path, "name": posixpath.basename(rel_path)})
        yield GraphEdge("HAS_FILE", "Project", {"id": proj_id}, "File", {"id": fid})

        for b in parsed.blocks:
            rid = ids.resource_id(proj_id, b.address)
            yield GraphNode("Resource", {"id": rid}, {
                "block_type": b.block_type, "rtype": b.rtype, "name": b.name,
                "address": b.address, "category": _category(b),
            })
            yield GraphEdge("HAS_RESOURCE", "File", {"id": fid}, "Resource", {"id": rid})

            for ref in b.images:
                yield from _emit_image(ref)
                yield GraphEdge("RUNS_IMAGE", "Resource", {"id": rid}, "Image", {"id": image_id(ref)})

            if b.rtype in _DB_TYPES:
                did = ids.database_id(proj_id, b.address)
                yield GraphNode("Database", {"id": did}, {"name": b.name, "rtype": b.rtype})
                yield GraphEdge("PROVISIONS", "Resource", {"id": rid}, "Database", {"id": did})
            elif b.rtype in _REGISTRY_TYPES:
                reg = ids.registry_id(proj_id, b.address)
                yield GraphNode("Registry", {"id": reg}, {"name": b.name, "rtype": b.rtype})
                yield GraphEdge("PROVISIONS", "Resource", {"id": rid}, "Registry", {"id": reg})

        for b in parsed.blocks:
            rid = ids.resource_id(proj_id, b.address)
            for ref in b.refs:
                target = addr_to_id.get(ref)
                if target and target != rid:
                    yield GraphEdge("DEPENDS_ON", "Resource", {"id": rid}, "Resource", {"id": target})


def _emit_image(ref: str) -> Iterator[GraphNode]:
    r = parse_image_ref(ref)
    yield GraphNode(
        "Image",
        {"id": image_id(ref)},
        {
            "ref": r.normalized, "registry": r.registry, "repository": r.repository,
            "tag": r.tag, "digest": r.digest, "resolved": False, "origin": "registry",
            **({"raw": r.raw} if r.unresolved else {}),
        },
    )
