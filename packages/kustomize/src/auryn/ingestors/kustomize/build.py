"""Turn parsed Kustomizations into the deployment-tier graph (ADR 0007): a `kustomization.yaml` File
owns a `Resource` (the overlay/base); the overlay RUNS_IMAGE the shared `Image` hub for each ref its
`images:` transformer sets, and DEPENDS_ON the base kustomizations it layers on (a `resources:`/`bases:`
/`components:` entry that resolves to another in-repo kustomization directory).

Emits no cross-artifact edge beyond the shared `Image` hub; the `deploy-kustomize` enrichment runs the
image lifecycle. v1 is render-free -- no `kustomize build`."""

from __future__ import annotations

import posixpath
from collections.abc import Iterator

from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.core.image_ref import image_id, image_node
from auryn.ingestors.kustomize import ids
from auryn.ingestors.kustomize.model import ParsedKustomization


def _resolve_base(kustomization_dir: str, entry: str) -> str:
    """Normalize a `resources:`/`bases:` path (relative to the kustomization dir) to a repo-relative
    directory key, so it can be matched against the known kustomization dirs. The repo-root kustomization
    is keyed by `""`, so a reference that resolves to the root (`posixpath.normpath` -> `"."`) maps to
    `""`. A remote/URL entry normalizes to something that matches nothing -- and is therefore ignored."""
    joined = posixpath.join(kustomization_dir, entry) if kustomization_dir else entry
    norm = posixpath.normpath(joined).replace(chr(92), "/")
    return "" if norm == "." else norm.strip("/")


def build_graph(kustomizations: list[tuple[str, ParsedKustomization]], proj_id: str) -> Iterator[GraphNode | GraphEdge]:
    # Index VALID kustomization dir -> its Resource id, so a base reference resolves to a real
    # DEPENDS_ON (a reference to a plain manifest file or a remote base matches nothing and is skipped).
    by_dir: dict[str, str] = {
        pk.kustomization_dir: ids.kustomization_id(proj_id, ids.kustomization_scope(pk.kustomization_dir))
        for _fp, pk in kustomizations if pk.valid
    }

    for file_path, pk in kustomizations:
        scope = ids.kustomization_scope(pk.kustomization_dir)
        kid = ids.kustomization_id(proj_id, scope)
        fid = ids.file_id(proj_id, file_path)

        yield GraphNode("File", {"id": fid}, {"path": file_path, "name": posixpath.basename(file_path)})
        yield GraphEdge("HAS_FILE", "Project", {"id": proj_id}, "File", {"id": fid})
        yield GraphNode("Resource", {"id": kid}, {
            "name": pk.kustomization_dir or "_root", "kind": "kustomization",
            "kustomization_dir": pk.kustomization_dir, "namespace": pk.namespace,
            "name_prefix": pk.name_prefix, "name_suffix": pk.name_suffix,
            "resolved": pk.valid,  # an unreadable / wrong-kind kustomization.yaml isn't usable
        })
        yield GraphEdge("HAS_RESOURCE", "File", {"id": fid}, "Resource", {"id": kid})

        for ref in pk.images:
            yield image_node(ref)
            yield GraphEdge("RUNS_IMAGE", "Resource", {"id": kid}, "Image", {"id": image_id(ref)})

        for base in pk.bases:
            target_dir = _resolve_base(pk.kustomization_dir, base)
            target = by_dir.get(target_dir)
            if target is not None and target != kid:  # resolves to another in-repo kustomization
                yield GraphEdge("DEPENDS_ON", "Resource", {"id": kid}, "Resource", {"id": target})
