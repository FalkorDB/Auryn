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


def _split_ref(ref: str) -> tuple[str, str, str]:
    """`repo[:tag][@digest]` -> (repository, tag, digest). The tag is a `:` in the LAST path segment
    only, so a `registry:port/...` host is never mistaken for a tag."""
    body, _, digest = ref.partition("@")
    cut = body.rfind("/")
    seg = body[cut + 1:]
    if ":" in seg:
        repo_tail, _, tag = seg.rpartition(":")
        return body[:cut + 1] + repo_tail, tag, digest
    return body, "", digest


def _resolve_image(entry: dict) -> str | None:
    """A single `images:` entry -> the concrete ref the overlay deploys, or None. Precedence follows
    Kustomize: `newName` replaces the repository, `newTag`/`digest` replace the tag/digest, and a rename
    with no new tag KEEPS the tag/digest carried by the matched `name`. Only an entry that ASSERTS a
    concrete ref is emitted -- a no-op `{name: x}`, or a `newName`-only rename whose base tag is unknown
    render-free, is skipped (materializing `:latest` would be a false hub join)."""
    if not isinstance(entry, dict):
        return None
    name, new_name = _scalar(entry.get("name")), _scalar(entry.get("newName"))
    new_tag, new_digest = _scalar(entry.get("newTag")), _scalar(entry.get("digest"))
    if not (new_name or new_tag or new_digest):
        return None  # a no-op entry -- just a match target, asserts nothing
    name_repo, name_tag, name_digest = _split_ref(name) if name else ("", "", "")
    repo = new_name or name_repo  # a tagged `name` contributes only its repository here
    if not repo:
        return None
    if new_digest:
        ref = f"{repo}@{new_digest}"
    elif new_tag:
        ref = f"{repo}:{new_tag}"
    elif name_digest:
        ref = f"{repo}@{name_digest}"  # renamed, digest kept from the matched name
    elif name_tag:
        ref = f"{repo}:{name_tag}"      # renamed, tag kept from the matched name
    else:
        return None  # renamed but the base's tag is unknown statically -- don't guess `:latest`
    return ref if _plausible_image(ref) else None


def _paths(doc: dict, key: str) -> list[str]:
    raw = doc.get(key)  # a malformed `resources: true` / scalar must not iterate (char-by-char or raise)
    out = []
    for p in raw if isinstance(raw, list) else []:
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
    raw_images = doc.get("images")  # a malformed `images: 42` must not iterate
    for entry in raw_images if isinstance(raw_images, list) else []:
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
