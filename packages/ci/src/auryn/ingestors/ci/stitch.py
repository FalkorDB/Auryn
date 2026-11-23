"""The `deploy-ci` stitch (ADR 0007): the CI producer writes its own `BUILDS_IMAGE` edges, so this arm
runs the shared image lifecycle -- recompute each `Image`'s `resolved` (a CI-built image that some
producer also runs now flips internal) and GC orphan images -- so the build->image->run loop is closed
no matter which arm ran last."""

from __future__ import annotations

import logging

from auryn.core import deploy
from auryn.core.graph_client import GraphWriter

logger = logging.getLogger("auryn.deploy-ci")


def run(writer: GraphWriter, repo, target_sha: str) -> dict | None:
    return stitch(writer)


def stitch(writer: GraphWriter) -> dict | None:
    try:
        if not writer.ro_query("MATCH (i:Image) RETURN i.id LIMIT 1").result_set:
            return None
    except Exception as e:
        if "empty key" in str(e).lower():
            return None
        raise
    deploy.recompute_image_resolved(writer)
    orphans = deploy.gc_orphan_images(writer)
    if not orphans:
        return None
    logger.info("deploy-ci: %d orphan image(s) GC'd", orphans)
    return {"orphans_removed": orphans}
