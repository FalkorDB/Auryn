"""Turn parsed compose files into the deployment-tier graph (ADR 0007): File -> Service, each service
RUNS_IMAGE the shared `Image` hub (from its `image:`) and DEPENDS_ON the services it waits on. A
service that builds stores the repo-relative Dockerfile path the `deploy-compose` stitch resolves to a
`BuildStage` (for `BUILDS_IMAGE`).

Emits the `Image` hub nodes `resolved:false` -- the stitch flips an image to `resolved:true` once a
`BUILDS_IMAGE` proves it internal. Emits no cross-artifact edge (the producer never reaches outside its
own files)."""

from __future__ import annotations

import posixpath
from collections.abc import Iterator

from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.core.image_ref import image_id, parse_image_ref
from auryn.ingestors.compose import ids
from auryn.ingestors.compose.model import ParsedCompose, Service


def _dockerfile_path(compose_dir: str, svc: Service) -> str | None:
    """The repo-relative path of the Dockerfile a service builds, or None if it doesn't build. `build:`
    context is relative to the COMPOSE FILE's directory; the Dockerfile defaults to `Dockerfile` inside
    that context."""
    if svc.build_context is None:
        return None
    ctx = svc.build_context.strip()
    ctx = ctx[2:] if ctx.startswith("./") else ctx
    dockerfile = (svc.build_dockerfile or "Dockerfile").strip()
    rel = posixpath.join(ctx, dockerfile)
    joined = posixpath.join(compose_dir, rel) if compose_dir else rel
    return posixpath.normpath(joined)


def build_graph(files: list[tuple[str, ParsedCompose]], proj_id: str) -> Iterator[GraphNode | GraphEdge]:
    for rel_path, parsed in files:
        fid = ids.file_id(proj_id, rel_path)
        compose_dir = posixpath.dirname(rel_path)
        yield GraphNode("File", {"id": fid}, {"path": rel_path, "name": posixpath.basename(rel_path)})
        yield GraphEdge("HAS_FILE", "Project", {"id": proj_id}, "File", {"id": fid})

        svc_ids = {s.name: ids.service_id(fid, s.name) for s in parsed.services}
        for svc in parsed.services:
            sid = svc_ids[svc.name]
            dockerfile = _dockerfile_path(compose_dir, svc)
            props: dict[str, object] = {
                "name": svc.name,
                "ports": list(svc.ports),
            }
            if dockerfile is not None:
                props["build_dockerfile"] = dockerfile
            yield GraphNode("Service", {"id": sid}, props)
            yield GraphEdge("HAS_SERVICE", "File", {"id": fid}, "Service", {"id": sid})

            if svc.image:
                yield from _emit_image(svc.image)
                yield GraphEdge("RUNS_IMAGE", "Service", {"id": sid}, "Image", {"id": image_id(svc.image)})

            for dep in svc.depends_on:
                if dep in svc_ids and svc_ids[dep] != sid:
                    yield GraphEdge("DEPENDS_ON", "Service", {"id": sid}, "Service", {"id": svc_ids[dep]})


def _emit_image(ref: str) -> Iterator[GraphNode]:
    r = parse_image_ref(ref)
    yield GraphNode(
        "Image",
        {"id": image_id(ref)},
        {
            "ref": r.normalized,
            "registry": r.registry,
            "repository": r.repository,
            "tag": r.tag,
            "digest": r.digest,
            "resolved": False,  # the deploy-compose stitch flips this true once a BUILDS_IMAGE appears
            "origin": "registry",
            **({"raw": r.raw} if r.unresolved else {}),
        },
    )
