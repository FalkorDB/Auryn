"""Container image-reference normalization -- the single most important consistency point of the
deployment tier (ADR 0007 §2). A Dockerfile `FROM python:3.12-slim`, a Compose `image:
python:3.12-slim`, and a k8s `image: python:3.12-slim` must all produce the SAME `Image` id, so the
image is the one hub node a build (`BUILDS_IMAGE`) and a run (`RUNS_IMAGE`) converge on across
repos. Keep this function the ONE place normalization lives.

NOTE (ADR 0007 §7 follow-up): this lives in the docker plugin for the first scaffold; when the
compose/k8s/terraform producers land it should migrate to a shared `auryn.core.image_ref` so every
IaC producer imports the identical rules instead of re-implementing them.

Docker reference grammar (the subset we normalize):

    [registry[:port]/]repository[:tag][@digest]

Rules (Docker's own defaults):
- default registry `docker.io`; a single-component official repo gets the `library/` namespace
  (`nginx` -> `docker.io/library/nginx`);
- default tag `latest` when neither a tag nor a digest is given;
- registry host + repository are lowercased (case-insensitive); the TAG is case-sensitive;
- a digest, when present, is the stronger identity and is kept (alongside any tag, which is dropped
  from the id -- the digest wins).
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_REGISTRY = "docker.io"
DEFAULT_TAG = "latest"
OFFICIAL_NAMESPACE = "library"

# Docker Hub answers under several host spellings; all fold onto DEFAULT_REGISTRY so the same image
# written as `index.docker.io/x` or `docker.io/x` keys ONE Image node.
_DOCKER_HUB_HOSTS = frozenset({"docker.io", "index.docker.io", "registry-1.docker.io"})

# `FROM scratch` is the explicit empty base -- not a real image, so it never becomes an Image node.
SCRATCH = "scratch"


@dataclass(frozen=True)
class ImageRef:
    registry: str
    repository: str
    tag: str | None
    digest: str | None
    raw: str
    # True when the ref contained an unexpanded build-arg/variable (`${BASE}`/`$TAG`) we couldn't
    # statically resolve -- the Image is emitted `resolved:false` with `raw` kept so a later pass
    # (a Compose/CI var substitution) can heal it. (ADR 0007 §2.)
    unresolved: bool

    @property
    def normalized(self) -> str:
        base = f"{self.registry}/{self.repository}"
        if self.digest:
            return f"{base}@{self.digest}"
        return f"{base}:{self.tag or DEFAULT_TAG}"


def _looks_like_registry(component: str) -> bool:
    """The first slash-separated component is a registry host (not a repo namespace) iff it carries a
    `.`/`:` or is `localhost` -- Docker's own heuristic (`docker.io`, `ghcr.io`, `localhost:5000`)."""
    return "." in component or ":" in component or component == "localhost"


def parse_image_ref(ref: str) -> ImageRef:
    raw = ref.strip()
    unresolved = "$" in raw  # an unexpanded ${VAR}/$VAR -- best-effort normalize, flag for healing

    body, _, digest = raw.partition("@")
    digest = digest or None

    first, slash, rest = body.partition("/")
    if slash and _looks_like_registry(first):
        # Lowercase the host up front (registries are case-insensitive) so the DEFAULT_REGISTRY
        # comparison below is reliable -- `DOCKER.IO/nginx` must key the same node as `docker.io/nginx`
        # (and get the `library/` namespace) -- and fold Docker Hub's legacy hosts onto docker.io.
        host = first.lower()
        registry = DEFAULT_REGISTRY if host in _DOCKER_HUB_HOSTS else host
        remainder = rest
    else:
        registry, remainder = DEFAULT_REGISTRY, body

    # The tag is a `:` in the LAST path segment only (so a `registry:port/...` host is never mistaken
    # for a tag, and a `@digest` already stripped above can't interfere).
    tag: str | None = None
    last_sep = remainder.rfind("/")
    seg = remainder[last_sep + 1 :]
    if ":" in seg:
        repo_tail, tag = seg.rsplit(":", 1)
        remainder = remainder[: last_sep + 1] + repo_tail

    repository = remainder
    if registry == DEFAULT_REGISTRY and "/" not in repository:
        repository = f"{OFFICIAL_NAMESPACE}/{repository}"

    if digest is None and tag is None:
        tag = DEFAULT_TAG

    return ImageRef(
        registry=registry.lower(),
        # A resolved repository is case-folded (registries are case-insensitive); an UNRESOLVED ref
        # keeps its case so a `${BASE_IMAGE}` variable name isn't mangled to `${base_image}` -- the
        # placeholder's identity must round-trip to the raw text a later substitution will expand.
        repository=repository if unresolved else repository.lower(),
        tag=tag,  # case-sensitive -- NOT lowered
        digest=digest,
        raw=raw,
        unresolved=unresolved,
    )


def normalize_image_ref(ref: str) -> str:
    """`nginx` -> `docker.io/library/nginx:latest`; `ghcr.io/acme/api:1.2` -> unchanged (lowered)."""
    return parse_image_ref(ref).normalized


def image_id(ref: str) -> str:
    """The graph id of an Image node: `image://<normalized-ref>` (ADR 0007 §2). Project-independent
    on purpose -- it is the key two repos / two ingestors agree on without coordination."""
    return f"image://{normalize_image_ref(ref)}"
