"""Turn parsed Dockerfiles into the deployment-tier graph (ADR 0007): File -> BuildStage, each stage
BASED_ON its base Image (or an earlier stage) and COPIES_FROM earlier stages. Emits the shared
`Image` hub nodes for every base reference as `resolved:false` placeholders -- the `deploy` stitch
later recomputes `resolved` (an image is resolved once something BUILDS_IMAGE it) and GCs orphans.

Does NOT emit the Project node (git_sync owns it, like the other ingestors) nor any cross-artifact
edge (`BUILDS_IMAGE`/`PACKAGES`/`SHIPS` are the stitch's -- the producer never reaches outside its
own files; ADR 0007 §7)."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterator

from auryn.core.graph_model import GraphEdge, GraphNode
from auryn.ingestors.docker import ids
from auryn.ingestors.docker.image_ref import SCRATCH, image_id, parse_image_ref
from auryn.ingestors.docker.model import ParsedDockerfile, Stage

# `${NAME}` or `$NAME` only -- a `${NAME:-default}` / `${NAME-x}` / `${NAME+x}` / `${NAME?x}` modifier
# form (anything but a bare identifier inside the braces) is intentionally NOT matched, so it is left
# verbatim and flagged unresolved rather than half-substituted into a corrupt ref.
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def _resolve_base(base: str, args: dict[str, str]) -> str:
    """Best-effort `${ARG}`/`$ARG` substitution from the file's global ARGs so `FROM ${BASE}:${TAG}`
    normalizes when those ARGs have NON-EMPTY defaults. A missing ARG, an empty-valued ARG (`ARG BASE`
    with no default), or a modifier form is left as the raw `${...}` text -- so the base stays flagged
    `unresolved` (and keeps a real Image placeholder) instead of collapsing to an empty/garbage ref."""
    if "$" not in base:
        return base

    def _sub(m: re.Match) -> str:
        name = m.group(1) if m.group(1) is not None else m.group(2)
        val = args.get(name)
        return val if val else m.group(0)  # missing or empty -> keep the raw ${...}/$NAME

    return _VAR.sub(_sub, base)


def _stage_key(stage: Stage) -> str:
    return stage.name if stage.name else str(stage.index)


def build_graph(files: list[tuple[str, ParsedDockerfile]], proj_id: str) -> Iterator[GraphNode | GraphEdge]:
    for rel_path, parsed in files:
        fid = ids.file_id(proj_id, rel_path)
        yield GraphNode("File", {"id": fid}, {"path": rel_path, "name": posixpath.basename(rel_path)})
        yield GraphEdge("HAS_FILE", "Project", {"id": proj_id}, "File", {"id": fid})

        # Resolve intra-file stage references (base + `COPY --from`) by name AND by index.
        stage_ids = {_stage_key(s): ids.stage_id(fid, _stage_key(s)) for s in parsed.stages}
        for s in parsed.stages:
            stage_ids.setdefault(str(s.index), ids.stage_id(fid, _stage_key(s)))

        for stage in parsed.stages:
            yield from _emit_stage(stage, parsed, fid, stage_ids)


def _emit_stage(stage: Stage, parsed: ParsedDockerfile, fid: str, stage_ids: dict[str, str]) -> Iterator[GraphNode | GraphEdge]:
    sid = ids.stage_id(fid, _stage_key(stage))
    # Host-context COPY sources (those WITHOUT --from) drive the code seam -- stored for the stitch.
    copy_sources = [src for c in stage.copies if c.from_stage is None for src in c.sources]
    yield GraphNode(
        "BuildStage",
        {"id": sid},
        {
            "name": stage.name or "",
            "stage": _stage_key(stage),
            "index": stage.index,
            "base_ref": stage.base,
            "dockerfile": posixpath.basename(fid.split("/file/", 1)[-1]) if "/file/" in fid else fid,
            "context": posixpath.dirname(fid.split("/file/", 1)[-1]) if "/file/" in fid else "",
            "copy_sources": copy_sources,
            "exposed_ports": list(stage.exposed_ports),
            "entrypoint": stage.entrypoint,
            "cmd": stage.cmd,
            "workdir": stage.workdir,
        },
    )
    yield GraphEdge("HAS_STAGE", "File", {"id": fid}, "BuildStage", {"id": sid})

    # BASED_ON: an earlier stage in this file, or an external base Image (`scratch` has neither).
    base = _resolve_base(stage.base, parsed.args)
    if base in stage_ids and stage_ids[base] != sid:
        yield GraphEdge("BASED_ON", "BuildStage", {"id": sid}, "BuildStage", {"id": stage_ids[base]},
                        {"resolution": "declared"})
    elif base and base != SCRATCH:
        yield from _emit_base_image(base)
        yield GraphEdge("BASED_ON", "BuildStage", {"id": sid}, "Image", {"id": image_id(base)},
                        {"resolution": "declared"})

    # COPIES_FROM: multi-stage `COPY --from=<earlier stage>` (intra-file only in v1).
    for c in stage.copies:
        if c.from_stage and c.from_stage in stage_ids and stage_ids[c.from_stage] != sid:
            yield GraphEdge("COPIES_FROM", "BuildStage", {"id": sid}, "BuildStage", {"id": stage_ids[c.from_stage]})


def _emit_base_image(ref: str) -> Iterator[GraphNode]:
    """A base `Image` as a shared, project-independent node (`image://<normalized>`), emitted
    `resolved:false` -- the stitch flips it true iff something BUILDS_IMAGE it. MERGE-dedup means the
    same base used by many Dockerfiles across repos is ONE node (the shared-base signal)."""
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
            "resolved": False,
            "origin": "registry",  # an external pull until a BUILDS_IMAGE proves it internal
            **({"raw": r.raw} if r.unresolved else {}),
        },
    )
