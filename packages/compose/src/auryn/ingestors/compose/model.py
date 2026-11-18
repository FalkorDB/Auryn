"""Parsed docker-compose data model (engine-agnostic). `file_parser.parse_source` produces these;
`build.build_graph` turns them into graph nodes/edges."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Service:
    """One compose service. `image` is the ref it runs; `build_context`/`build_dockerfile` (when the
    service builds) let the stitch resolve the Dockerfile that PRODUCES that image. `depends_on` is the
    service names it waits on."""

    name: str
    image: str | None = None
    build_context: str | None = None
    build_dockerfile: str | None = None  # relative to the context; defaults to "Dockerfile"
    depends_on: tuple[str, ...] = ()
    ports: tuple[str, ...] = ()


@dataclass(frozen=True)
class ParsedCompose:
    services: tuple[Service, ...] = ()
