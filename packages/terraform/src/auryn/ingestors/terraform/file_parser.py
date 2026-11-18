"""Parse Terraform/OpenTofu (`.tf`) -> `ParsedTf` via python-hcl2.

hcl2 v4 emits block labels as quoted strings and keeps attribute values in HCL form (`"x"`, `${ref}`),
with an `__is_block__` marker -- so we unquote labels, scan string values for `${...}` references
(-> DEPENDS_ON) and for container image refs (-> the hub seam). Best-effort and never fatal: a file
that fails to parse yields nothing (git_sync logs it)."""

from __future__ import annotations

import io
import re

import hcl2

from auryn.ingestors.terraform.model import Block, ParsedTf

# A Terraform reference inside `${...}`: a resource (`type.name`, type has an underscore), a module
# (`module.name`), or a data source (`data.type.name`). `var.`/`local.` are intentionally NOT blocks.
_REF = re.compile(
    r"(?<![\w.])(module\.[A-Za-z_][\w-]*|data\.[A-Za-z_]\w*\.[A-Za-z_][\w-]*|[A-Za-z_]\w*_[A-Za-z_]\w*\.[A-Za-z_][\w-]*)"
)
# `image = "x"` / `"image": "x"` inside an attribute (e.g. a container_definitions jsonencode blob).
_IMAGE = re.compile(r'"?image(?:_uri)?"?\s*[:=]\s*"([^"${}\s]+)"')

_SKIP = "__is_block__"


def _unq(s):
    return s[1:-1] if isinstance(s, str) and len(s) >= 2 and s[0] == '"' and s[-1] == '"' else s


def _strings(v):
    """Every string leaf under an attribute value (dicts/lists walked)."""
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for k, x in v.items():
            if k != _SKIP:
                yield from _strings(x)
    elif isinstance(v, list):
        for x in v:
            yield from _strings(x)


def _address(ref: str) -> str:
    parts = ref.split(".")
    if ref.startswith("module."):
        return f"module.{parts[1]}"
    if ref.startswith("data."):
        return f"data.{parts[1]}.{parts[2]}"
    return f"{parts[0]}.{parts[1]}"  # resource: type.name


def _refs(attrs: dict, self_address: str) -> tuple[str, ...]:
    out: dict[str, None] = {}
    for s in _strings(attrs):
        if "." not in s:
            continue
        for m in _REF.findall(s):
            addr = _address(m)
            if addr != self_address:
                out.setdefault(addr, None)
    return tuple(out)


def _images(attrs: dict) -> tuple[str, ...]:
    out: dict[str, None] = {}
    for k, v in attrs.items():
        if k in ("image", "image_uri") and isinstance(v, str):
            img = _unq(v)
            if img:
                out.setdefault(img, None)
    for s in _strings(attrs):
        for m in _IMAGE.findall(s):
            out.setdefault(m, None)
    return tuple(out)


def _blocks(doc: dict, block_type: str) -> list[Block]:
    out: list[Block] = []
    for entry in doc.get(block_type, []):
        if not isinstance(entry, dict):
            continue
        if block_type == "module":
            for name, attrs in entry.items():
                attrs = attrs if isinstance(attrs, dict) else {}
                addr = f"module.{_unq(name)}"
                out.append(Block("module", "", _unq(name), addr, _refs(attrs, addr), _images(attrs)))
        else:  # resource | data
            for rtype, named in entry.items():
                if not isinstance(named, dict):
                    continue
                rtype = _unq(rtype)
                for name, attrs in named.items():
                    attrs = attrs if isinstance(attrs, dict) else {}
                    name = _unq(name)
                    addr = f"{rtype}.{name}" if block_type == "resource" else f"data.{rtype}.{name}"
                    out.append(Block(block_type, rtype, name, addr, _refs(attrs, addr), _images(attrs)))
    return out


def parse_source(data: str | bytes) -> ParsedTf:
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="replace")
    try:
        doc = hcl2.load(io.StringIO(data))
    except Exception:
        return ParsedTf()
    if not isinstance(doc, dict):
        return ParsedTf()
    blocks: list[Block] = []
    for bt in ("resource", "data", "module"):
        blocks.extend(_blocks(doc, bt))
    return ParsedTf(tuple(blocks))
