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

# A Terraform reference: a resource (`type.name`, type has an underscore), a module (`module.name`),
# or a data source (`data.type.name`). `var.`/`local.` are intentionally NOT blocks.
_REF = re.compile(
    r"(?<![\w.])(module\.[A-Za-z_][\w-]*|data\.[A-Za-z_]\w*\.[A-Za-z_][\w-]*|[A-Za-z_]\w*_[A-Za-z_]\w*\.[A-Za-z_][\w-]*)"
)
# `image = "x"` / `"image": "x"` inside a serialized blob (e.g. a container_definitions jsonencode).
# The value may itself be an interpolation (`"${ecr_url}:tag"`) -- keep it whole; the shared
# `image_node` flags it unresolved. It is bounded by the surrounding quotes, so stop only at a quote
# or whitespace (an image ref never contains either).
_IMAGE = re.compile(r'"?image(?:_uri)?"?\s*[:=]\s*"([^"\s]+)"')


def _interpolations(s: str):
    """Yield the CONTENT of each top-level `${...}` in `s`, brace-BALANCED and QUOTE-aware (so a
    `}` inside a quoted argument -- `${lookup(x, "a}b")}` -- does not end the interpolation early)
    and skipping an escaped `$${` (which Terraform treats as a literal `${`). References live only
    here -- a quoted string literal like `"aws_x.y"` is NOT an interpolation and must not fabricate
    a reference (see `_expr_refs`)."""
    i, n = 0, len(s)
    while i < n - 1:
        if s[i] == "$" and s[i + 1] == "{":
            if i > 0 and s[i - 1] == "$":  # `$${` -> literal, not an interpolation
                i += 2
                continue
            depth, j, in_str, esc = 1, i + 2, False, False
            while j < n and depth:
                c = s[j]
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif in_str:
                    in_str = c != '"'
                elif c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                j += 1
            yield s[i + 2 : j - 1]
            i = j
        else:
            i += 1


def _split_quoted(s: str):
    """Yield `(segment, is_quoted)` runs of `s`, splitting on double-quoted spans (respecting an
    escaped `\\"`). A quoted segment is the text BETWEEN the quotes; surrounding expression text
    comes back as `is_quoted=False`."""
    i, n, start = 0, len(s), 0
    while i < n:
        if s[i] == '"':
            if i > start:
                yield s[start:i], False
            j = i + 1
            while j < n and s[j] != '"':
                j += 2 if s[j] == "\\" else 1
            yield s[i + 1 : j], True
            i = start = j + 1
        else:
            i += 1
    if start < n:
        yield s[start:], False


def _expr_refs(content: str):
    """Addresses referenced in an interpolation's EXPRESSION. A quoted string literal is not an
    expression -- its literal text (even if it looks like `aws_x.y`) can't be a reference -- but a
    string may embed further `${...}` interpolations, which ARE expressions, so recurse into those."""
    for seg, quoted in _split_quoted(content):
        if quoted:
            for inner in _interpolations(seg):  # a nested ${...} inside the string literal
                yield from _expr_refs(inner)
        else:
            yield from _REF.findall(seg)


def _unq(s):
    return s[1:-1] if isinstance(s, str) and len(s) >= 2 and s[0] == '"' and s[-1] == '"' else s


def _meta_key(k) -> bool:
    """hcl2 v4 injects bookkeeping keys (`__is_block__`, `__start_line__`, comments, ...) -- skip any
    `__`-prefixed key so metadata is never scanned as Terraform data."""
    return isinstance(k, str) and k.startswith("__")


def _strings(v):
    """Every string leaf under an attribute value (dicts/lists walked; hcl2 metadata keys skipped)."""
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for k, x in v.items():
            if not _meta_key(k):
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
        for interp in _interpolations(s):  # only ${...} content, not plain string literals
            for m in _expr_refs(interp):   # ... and only the EXPRESSION, not quoted literals within
                addr = _address(m)
                if addr != self_address:
                    out.setdefault(addr, None)
    return tuple(out)


# Container-RUNTIME resources whose root carries a top-level `image` with no nested container block.
# An explicit allow-list, NOT an `endswith("_container")` test -- `azurerm_storage_container` and the
# like manage storage, not workloads, and must not have a `metadata.image` read as a container image.
_CONTAINER_RESOURCE_TYPES = frozenset({"docker_container", "podman_container"})

# Image-ref attributes that are container-specific wherever they appear: Lambda/ECS `image_uri`, App
# Runner's `source_configuration.image_repository.image_identifier`.
_IMAGE_ATTRS = frozenset({"image_uri", "image_identifier"})

# Nested block keys that open a container context for a bare `image` (Cloud Run `template.containers`,
# the k8s provider's `spec.container` / `spec.init_container`).
_CONTAINER_BLOCK_KEYS = frozenset({"container", "containers", "init_container"})


def _is_container_resource(rtype: str) -> bool:
    """Whether a resource TYPE denotes a container runtime whose root attributes ARE a container context
    (the Docker provider's `docker_container` carries a top-level `image` with no nested block)."""
    return rtype in _CONTAINER_RESOURCE_TYPES


def _collect_image_keys(v, out: dict, in_container: bool = False) -> None:
    """Find CONTAINER image refs at any nesting. An `_IMAGE_ATTRS` key (`image_uri`/`image_identifier`)
    is container-specific, so collected anywhere; a bare `image` is collected ONLY inside a container
    context -- a `_CONTAINER_BLOCK_KEYS` block (Cloud Run's `template.containers.image`, the k8s
    provider's `spec.container`/`spec.init_container`) or a container-runtime resource root. So a VM boot
    disk's `boot_disk.initialize_params.image` is NOT mistaken for a container image, and a metadata map
    keyed `container_owner` does NOT turn the context on (the key must BE one of the block keys, not
    merely contain the word). Interpolated (`${...}`) values are KEPT -- `image_node` flags them
    unresolved -- so `image_uri = "${ecr...}:tag"` still yields an Image."""
    if isinstance(v, dict):
        for k, x in v.items():
            if _meta_key(k):
                continue
            if (k in _IMAGE_ATTRS or (k == "image" and in_container)) and isinstance(x, str):
                img = _unq(x)
                if img:
                    out.setdefault(img, None)
            else:
                child_container = in_container or (isinstance(k, str) and k.lower() in _CONTAINER_BLOCK_KEYS)
                _collect_image_keys(x, out, child_container)
    elif isinstance(v, list):
        for x in v:
            _collect_image_keys(x, out, in_container)


# Attributes that carry a serialized container spec (an ECS `container_definitions = jsonencode([...])`
# blob). The `_IMAGE` text scan runs ONLY over these -- scanning every string leaf would read an
# `image = "..."` line inside an unrelated `user_data` script as a container image.
_BLOB_KEYS = frozenset({"container_definitions"})


def _blob_strings(v):
    """String leaves under a `_BLOB_KEYS` attribute, at any nesting (the blob is usually a top-level
    attribute, but search defensively)."""
    if isinstance(v, dict):
        for k, x in v.items():
            if _meta_key(k):
                continue
            yield from (_strings(x) if isinstance(k, str) and k in _BLOB_KEYS else _blob_strings(x))
    elif isinstance(v, list):
        for x in v:
            yield from _blob_strings(x)


def _images(attrs: dict, rtype: str = "") -> tuple[str, ...]:
    """CONTAINER images a resource runs. Scope boundary (best-effort, not a full HCL evaluator): an
    image is materialized only when it has a LITERAL component -- a plain ref (`"shop/api:1.0"`) or an
    interpolation with literal text (`"${ecr.url}:1.0"`, kept unresolved). A bare variable-only value
    (`image = var.api_image`) is NOT materialized: it has no literal to key on and can never resolve to
    or join a concrete hub image. Deeply nested interpolation-in-quoted-string expressions
    (`${coalesce("${upper("}")}", ...)`) are likewise out of scope for the state-machine scanner."""
    out: dict[str, None] = {}
    _collect_image_keys(attrs, out, _is_container_resource(rtype))  # structured image keys at any depth
    for s in _blob_strings(attrs):                  # + images inside a container_definitions jsonencode blob
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
                    out.append(Block(block_type, rtype, name, addr, _refs(attrs, addr), _images(attrs, rtype)))
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
