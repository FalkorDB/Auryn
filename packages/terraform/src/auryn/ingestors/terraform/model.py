"""Parsed Terraform/OpenTofu model (engine-agnostic). `file_parser.parse_source` produces these;
`build.build_graph` turns them into graph nodes/edges."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Block:
    """A `resource` / `module` / `data` block. `address` is its Terraform address
    (`aws_db_instance.db`, `module.net`, `data.aws_ami.ubuntu`) -- the id other blocks reference.
    `refs` are the addresses it interpolates (`${aws_x.y.z}` -> `aws_x.y`), which become DEPENDS_ON.
    `images` are container image refs found in its attributes (the hub seam)."""

    block_type: str          # "resource" | "module" | "data"
    rtype: str               # resource/data type, e.g. "aws_db_instance"; "" for a module
    name: str
    address: str
    refs: tuple[str, ...] = ()
    images: tuple[str, ...] = ()


@dataclass(frozen=True)
class ParsedTf:
    blocks: tuple[Block, ...] = ()
