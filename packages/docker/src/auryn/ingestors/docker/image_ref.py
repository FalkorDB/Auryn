"""Image-reference normalization now lives in `auryn.core.image_ref` -- shared by every deployment-tier
producer so their `image://` ids are identical (ADR 0007 §7, migrated when the compose producer
landed). This module re-exports it for back-compat so existing
`auryn.ingestors.docker.image_ref` imports keep working."""

from __future__ import annotations

from auryn.core.image_ref import (  # noqa: F401
    DEFAULT_REGISTRY,
    DEFAULT_TAG,
    OFFICIAL_NAMESPACE,
    SCRATCH,
    ImageRef,
    image_id,
    normalize_image_ref,
    parse_image_ref,
)

__all__ = [
    "DEFAULT_REGISTRY", "DEFAULT_TAG", "OFFICIAL_NAMESPACE", "SCRATCH",
    "ImageRef", "parse_image_ref", "normalize_image_ref", "image_id",
]
