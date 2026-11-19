"""Parse a `kustomization.yaml` (render-free) -> `ParsedKustomization`. Pure, no graph.

The two tier-relevant declarations, read WITHOUT running `kustomize build`:
  * the `images:` transformer -- `{name, newName?, newTag?, digest?}` entries -> the concrete image an
    overlay deploys (the hub seam);
  * `resources:`/`bases:`/`components:` -- the paths this overlay layers on (the base structure).
Best-effort and never fatal: a malformed file yields an invalid `ParsedKustomization`."""

from __future__ import annotations

import yaml

from auryn.ingestors.kustomize.model import ParsedKustomization

# Kustomize recognizes a file as a kustomization by its basename regardless of `kind`, but a present
# `kind` that is NOT one of these means it's some other manifest that merely shares the name.
_KINDS = frozenset({"Kustomization", "Component", ""})


def _scalar(v) -> str:
    """A YAML scalar as a stripped string (bools lowercased); "" for a map/list/null."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (str, int, float)):
        return str(v).strip()
    return ""


def _plausible_image(ref: str) -> bool:
    """A sane-looking image ref: non-empty, no whitespace, and -- if the last path segment carries a
    `:tag` -- a non-empty repository and tag. Kustomize values are literal (no templating), but guard
    against a stray non-image string so `core.image_ref` can't mint a junk Image node."""
    if not ref or any(c.isspace() for c in ref):
        return False
    last = ref.split("@", 1)[0].rsplit("/", 1)[-1]
    if ":" in last:
        repo_tail, _, tag = last.rpartition(":")
        if not repo_tail or not tag:
            return False
    return True


def _resolve_image(entry: dict) -> str | None:
    """A single `images:` entry -> the concrete ref it sets, or None. `newName`/`newTag`/`digest`
    override the matched `name`; a bare `{name: x}` with no override keeps the base's render-time tag
    (unknown statically), so it is skipped -- only an entry that ASSERTS a concrete image is emitted."""
    if not isinstance(entry, dict):
        return None
    name, new_name = _scalar(entry.get("name")), _scalar(entry.get("newName"))
    new_tag, digest = _scalar(entry.get("newTag")), _scalar(entry.get("digest"))
    repo = new_name or name
    if not repo:
        return None
    if digest:
        ref = f"{repo}@{digest}"
    elif new_tag:
        ref = f"{repo}:{new_tag}"
    elif new_name:
        ref = repo  # repo replaced, tag kept from the base manifest (unknown render-free)
    else:
        return None  # a no-op entry (just a match target) -- asserts nothing concrete
    return ref if _plausible_image(ref) else None


def _paths(doc: dict, key: str) -> list[str]:
    out = []
    for p in doc.get(key, []) or []:
        s = _scalar(p)
        if s:
            out.append(s)
    return out


def parse_kustomization(kustomization_dir: str, data: str | bytes) -> ParsedKustomization:
    try:
        doc = yaml.safe_load(data)
    except Exception:
        return ParsedKustomization(kustomization_dir=kustomization_dir, valid=False)
    if not isinstance(doc, dict) or _scalar(doc.get("kind")) not in _KINDS:
        return ParsedKustomization(kustomization_dir=kustomization_dir, valid=False)

    images: dict[str, None] = {}
    for entry in doc.get("images", []) or []:
        ref = _resolve_image(entry)
        if ref:
            images.setdefault(ref, None)

    bases: dict[str, None] = {}
    for key in ("resources", "bases", "components"):
        for p in _paths(doc, key):
            bases.setdefault(p, None)

    return ParsedKustomization(
        kustomization_dir=kustomization_dir,
        valid=True,
        images=tuple(images),
        bases=tuple(bases),
        namespace=_scalar(doc.get("namespace")),
        name_prefix=_scalar(doc.get("namePrefix")),
        name_suffix=_scalar(doc.get("nameSuffix")),
    )
