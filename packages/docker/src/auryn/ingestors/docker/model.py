"""The parsed-Dockerfile data model (engine-agnostic). `file_parser.parse_source` produces these;
`build.build_graph` turns them into graph nodes/edges. Keeping this a plain dataclass tree (not
graph objects) means a future tree-sitter-dockerfile engine can swap in behind `parse_source`
without touching `build.py`."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Copy:
    """A `COPY`/`ADD` instruction. `from_stage` is the `--from=` source (another build stage or an
    external image) when present -- such a copy pulls from that stage/image, NOT from the host build
    context, so it becomes a `COPIES_FROM` edge rather than a host-file `PACKAGES` join."""

    sources: tuple[str, ...]
    dest: str
    from_stage: str | None = None


@dataclass(frozen=True)
class Stage:
    """One `FROM ... [AS name]` build stage and the instructions that follow it, up to the next
    `FROM`. `base` is the raw `FROM` argument -- an image reference OR the name/index of an earlier
    stage in the same file (multi-stage build)."""

    index: int
    base: str
    name: str | None = None
    copies: tuple[Copy, ...] = ()
    exposed_ports: tuple[str, ...] = ()
    entrypoint: str | None = None
    cmd: str | None = None
    workdir: str | None = None


@dataclass(frozen=True)
class ParsedDockerfile:
    stages: tuple[Stage, ...] = ()
    # Global `ARG`s declared before the first `FROM` (they can parameterize a `FROM ${ARG}` base).
    args: dict[str, str] = field(default_factory=dict)
