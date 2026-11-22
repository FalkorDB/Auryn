"""Parsed CI model (engine-agnostic). `file_parser.parse_workflow` produces a `ParsedWorkflow` per
GitHub Actions workflow; `build.build_graph` turns them into graph nodes/edges.

v1 is STATIC: the workflow YAML is read, but nothing is executed. The one tier-relevant thing it
carries is the set of images a workflow BUILDS/PUSHES -- the `BUILDS_IMAGE` seam that closes the
build->image->run loop (the edge the docker producer deliberately defers to CI/Compose, ADR 0007)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ParsedWorkflow:
    """One GitHub Actions workflow file. `valid` is False for a YAML file under `.github/workflows/`
    that isn't a usable workflow (unreadable, or missing a `jobs:` map). `name` is the workflow's
    declared `name:` (else its filename). `built_images` are the concrete image refs its steps build or
    push -- `docker/build-push-action` tags, or `docker build -t` / `docker push` in a `run:` step --
    keyed on the shared hub."""

    valid: bool = False
    name: str = ""
    built_images: tuple[str, ...] = ()
