"""Extract the container images and notable config values a chart's `values.yaml` declares -- the
STATIC, render-free seam (ADR 0007). Pure, no graph.

Images follow the common Helm conventions: an `image:` MAPPING with `repository` (+ optional
`registry`/`tag`), or a bare `image: "repo:tag"` STRING, at the top level or nested under any
sub-chart/component key (`primary.image.repository`, ...). The concrete ref is handed to
`core.image_ref`, so a chart's image converges on the same `Image` hub node docker/compose/k8s/tf key.

Config values are the scalar leaves down to a bounded depth -- the chart's notable static config, NOT
every leaf (deep maps and lists are skipped, keeping the node count sensible)."""

from __future__ import annotations

# Top-level + one nested level: captures `replicaCount`, `service.port`, `image.repository`/`image.tag`
# (the "top-level config keys and image-related keys"), while deeper trees (`resources.limits.cpu`,
# `ingress.annotations.*`) stay out -- a sensible config slice, not an exhaustive flatten.
_MAX_CONFIG_DEPTH = 2


def _scalar(v) -> str | None:
    """A YAML scalar as a string (bools lowercased `true`/`false`), or None for a map/list/null."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (str, int, float)):
        return str(v)
    return None


def _assemble_image(img: dict) -> str | None:
    """`{repository, tag?, registry?}` -> a concrete ref (`ghcr.io/acme/api:1.2`), or None without a
    `repository`. Tag/registry are optional; `core.image_ref` applies the registry/`latest` defaults."""
    repository = _scalar(img.get("repository"))
    if not repository:
        return None
    registry = _scalar(img.get("registry"))
    tag = _scalar(img.get("tag"))
    ref = f"{registry.rstrip('/')}/{repository}" if registry else repository
    return f"{ref}:{tag}" if tag else ref


def _plausible_image(ref: str) -> bool:
    """A sane-looking image ref: non-empty, no whitespace, no unrendered `{{...}}` template, and -- if
    the last path segment carries a `:tag` -- a non-empty repository and tag. Filters a prose value or a
    templated tag that happens to sit on an `image:` key (`image: "see the docs"`, `shop/api:`,
    `repository: shop/api, tag: "{{ .Chart.AppVersion }}"`); `core.image_ref` would otherwise mint a
    junk Image node from them."""
    if not ref or "{{" in ref or any(c.isspace() for c in ref):
        return False
    last = ref.split("@", 1)[0].rsplit("/", 1)[-1]  # last path segment, digest stripped
    if ":" in last:
        repo_tail, _, tag = last.rpartition(":")
        if not repo_tail or not tag:
            return False
    return True


def extract_images(values: dict) -> tuple[str, ...]:
    """Every container image ref declared under `values`, de-duplicated in first-seen order. Walks the
    whole tree: wherever a mapping has an `image` key (a `repository` sub-map, or a bare `repo:tag`
    string) a ref is collected -- if it looks like a real image ref (`_plausible_image`)."""
    out: dict[str, None] = {}

    def _add(ref: str | None) -> None:
        if ref and _plausible_image(ref):
            out.setdefault(ref, None)

    def walk(node) -> None:
        if isinstance(node, dict):
            img = node.get("image")
            if isinstance(img, str):
                _add(img.strip())
            elif isinstance(img, dict):
                _add(_assemble_image(img))
            for k, v in node.items():
                if k != "image":  # the `image` block itself holds only scalars -- no images nested within
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(values)
    return tuple(out)


def extract_config_values(values: dict) -> tuple[tuple[str, str], ...]:
    """`(dotted-name, value)` for the scalar leaves of `values` down to `_MAX_CONFIG_DEPTH` levels --
    the chart's notable static config (`replicaCount`, `service.port`, `image.tag`, ...). Lists and
    deeper maps are skipped so the node count stays sensible, not every leaf (ADR 0007)."""
    out: list[tuple[str, str]] = []

    def walk(node: dict, prefix: str, depth: int) -> None:
        if depth > _MAX_CONFIG_DEPTH:
            return
        for k, v in node.items():
            name = f"{prefix}.{k}" if prefix else str(k)
            s = _scalar(v)
            if s is not None:
                out.append((name, s))
            elif isinstance(v, dict):
                walk(v, name, depth + 1)
            # lists / nulls: skipped

    if isinstance(values, dict):
        walk(values, "", 1)
    return tuple(out)
