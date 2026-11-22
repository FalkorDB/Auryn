"""Parsed Kustomize model (engine-agnostic). `file_parser.parse_kustomization` produces a
`ParsedKustomization` per `kustomization.yaml`; `build.build_graph` turns them into graph nodes/edges.

v1 is STATIC and render-free: the `kustomization.yaml` is read, but `kustomize build` is NOT run, so
the base manifests it patches are not rendered. The two things that carry over the tier contract
without a render are the `images:` transformer (the concrete images an overlay deploys -> the hub) and
the `resources:`/`bases:`/`components:` references (the overlay -> base structure)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ParsedKustomization:
    """One `kustomization.yaml`. `kustomization_dir` is its directory relative to the repo root (the
    anchor) -- the scope of every id. `images` are the concrete refs its `images:` transformer sets
    (the hub seam). `bases` are the raw `resources:`/`bases:`/`components:` entries (relative paths);
    build resolves the ones that point at another in-repo kustomization into `DEPENDS_ON`. `valid` is
    False for a file that isn't a usable Kustomization (unreadable, or missing the Kustomization kind)."""

    kustomization_dir: str
    valid: bool = False
    images: tuple[str, ...] = ()
    bases: tuple[str, ...] = ()
    namespace: str = ""
    name_prefix: str = ""
    name_suffix: str = ""
