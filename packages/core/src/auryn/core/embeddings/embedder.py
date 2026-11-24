"""Pluggable text embedders for semantic code search.

The default is a **local** model run through fastembed's ONNX runtime -- no PyTorch. fastembed
is auto-installed on demand (opt-out, exactly like the live-DB drivers via
`core.plugins.ensure_importable`), so a base install stays lean and only pays for it the first
time embeddings are actually built.

The chosen model fixes the vector **dimension**, and a FalkorDB vector index is fixed-dimension.
So the model id + dim are treated as **graph-wide** facts (config), never per-node: switching the
embedder means dropping the index and re-embedding the whole graph. That keeps per-node storage
to just the vector -- no `embed_model`/`embed_dim`/`embed_hash` bookkeeping. The presence or
absence of the `embedding` property is itself the "needs embedding?" state.

Default: `jinaai/jina-embeddings-v2-base-code` -- code-trained, 768-dim, 8192-token context (long
enough to embed whole function/class bodies without truncation), Apache-2.0, and the only
code-specific model in fastembed's ONNX roster. An external API embedder (e.g. voyage-code-3) can
be added behind `get_embedder` later for higher quality, gated on data-egress consent.
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable

DEFAULT_LOCAL_MODEL = "jinaai/jina-embeddings-v2-base-code"
DEFAULT_LOCAL_DIM = 768


def embeddings_enabled(explicit: bool | None = None) -> bool:
    """Whether the ingest pipeline builds embeddings after a sync. **On by default.** Embeddings are
    built by a detached background daemon, off the ingest critical path (see embeddings.daemon), so
    they no longer slow a sync down -- there's no reason to make it an opt-in choice, and no install
    prompt for it. Resolved in precedence order:

    1. `explicit` -- an in-code override (wins over everything).
    2. `AURYN_EMBED` env var, when set to a non-empty value -- a low-level escape hatch
       (1/true/yes/on -> on; anything else, e.g. 0 -> off). Set `AURYN_EMBED=0` to turn the
       post-ingest embed off offline, in CI, or on a minimal install that can't fetch the model.
    3. default: on.
    """
    if explicit is not None:
        return explicit
    raw = os.environ.get("AURYN_EMBED")
    if raw is not None and raw.strip() != "":
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return True


def configured_model() -> str | None:
    """An embedding-model override from the environment (AURYN_EMBED_MODEL), or None for the
    default local model. Kept here so the CLI and the auto-embed hook agree on the source."""
    return os.environ.get("AURYN_EMBED_MODEL", "").strip() or None


EMBED_MODES = ("inline", "background", "off")

DEFAULT_EMBED_MODE = "background"
"""How the post-ingest embed pass runs when auto-embed is on. **background** by default: the
heavy ONNX compute is handed to a detached, single-flight daemon so the graph is returned (and is
queryable) the moment it's built, instead of the ingest call blocking on every vector. ``inline``
restores the old synchronous behaviour (used by the serverless/quickstart and explicit-``--host``
paths, whose ad-hoc connection a separate daemon couldn't reach); ``off`` skips embedding."""


def embed_mode(explicit: str | None = None) -> str:
    """Resolve how auto-embed runs, in the same precedence shape as `embeddings_enabled`:
    an in-code `explicit` override, then `AURYN_EMBED_MODE`, then the durable `[embeddings] mode`
    install setting, then `DEFAULT_EMBED_MODE`. An unrecognised value falls back to the default
    rather than raising -- a stray env/config typo must never break an ingest."""
    for candidate in (explicit, os.environ.get("AURYN_EMBED_MODE")):
        if candidate and candidate.strip().lower() in EMBED_MODES:
            return candidate.strip().lower()
        if candidate and candidate.strip():
            # A set-but-invalid value: ignore it (fall through to the next source), but don't crash.
            continue
    try:
        from auryn.core.app_config import load_config

        configured = (load_config().embed_mode or "").strip().lower()
        if configured in EMBED_MODES:
            return configured
    except Exception:
        pass
    return DEFAULT_EMBED_MODE

# Embedding dimension for the models we support out of the box, so callers can size the vector
# index without importing (and downloading) the model just to ask. fastembed reports the true dim
# via list_supported_models(); `known_dim` falls back to that for anything not listed here.
_KNOWN_DIMS: dict[str, int] = {
    "jinaai/jina-embeddings-v2-base-code": 768,
    "jinaai/jina-embeddings-v2-small-en": 512,
    "BAAI/bge-small-en-v1.5": 384,
    "BAAI/bge-base-en-v1.5": 768,
    "sentence-transformers/all-MiniLM-L6-v2": 384,
    "nomic-ai/nomic-embed-text-v1.5": 768,
}


@runtime_checkable
class Embedder(Protocol):
    """Turns text into fixed-dimension vectors. `model_id` + `dim` identify the vector space;
    changing either invalidates every stored vector (see module docstring)."""

    model_id: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of documents. Returns one vector (list of floats) per input, in order."""

    def embed_query(self, text: str) -> list[float]:
        """Embed a single search query. Separate entry point so an asymmetric model can encode
        queries differently from documents; symmetric models just reuse `embed`."""


def known_dim(model_id: str, *, auto_install: bool | None = None) -> int:
    """The embedding dimension for `model_id`. Uses the static table for known models; otherwise
    asks fastembed's model registry (which may trigger the opt-out fastembed install)."""
    if model_id in _KNOWN_DIMS:
        return _KNOWN_DIMS[model_id]
    from auryn.core.plugins import ensure_importable

    fastembed = ensure_importable("fastembed", "fastembed>=0.4", auto_install=auto_install)
    for spec in fastembed.TextEmbedding.list_supported_models():
        if spec.get("model") == model_id:
            return int(spec["dim"])
    raise ValueError(f"Unknown embedding model {model_id!r}: not in the static table or fastembed's registry.")


class FastEmbedEmbedder:
    """Local embedder over fastembed's ONNX runtime (no torch). The model is loaded lazily on the
    first `embed`, so constructing one is cheap and import-safe; the (possibly large) model
    download happens only when embeddings are actually computed."""

    def __init__(
        self,
        model_id: str = DEFAULT_LOCAL_MODEL,
        dim: int | None = None,
        *,
        auto_install: bool | None = None,
    ) -> None:
        self.model_id = model_id
        self.dim = dim if dim is not None else _KNOWN_DIMS.get(model_id, DEFAULT_LOCAL_DIM)
        self._auto_install = auto_install
        self._model = None

    def _ensure_model(self):
        if self._model is None:
            from auryn.core.plugins import ensure_importable

            fastembed = ensure_importable("fastembed", "fastembed>=0.4", auto_install=self._auto_install)
            self._model = fastembed.TextEmbedding(model_name=self.model_id)
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._ensure_model()
        # fastembed returns an iterator of numpy float32 arrays (already L2-normalized for the
        # supported models); coerce to plain lists of Python floats so nothing numpy crosses into
        # the graph-write layer or a pickle boundary.
        return [[float(x) for x in vec] for vec in model.embed(texts)]

    def embed_query(self, text: str) -> list[float]:
        model = self._ensure_model()
        query_embed = getattr(model, "query_embed", None)
        if query_embed is not None:
            # Some fastembed models expose an asymmetric query encoder; use it when present.
            return [[float(x) for x in v] for v in query_embed([text])][0]
        return self.embed([text])[0]


def get_embedder(model_id: str | None = None, *, auto_install: bool | None = None) -> Embedder:
    """The configured embedder. Today always the local fastembed model; the seam is here so an
    external provider (selected by a scheme prefix like `voyage:...`) can be added without
    touching call sites."""
    return FastEmbedEmbedder(model_id or DEFAULT_LOCAL_MODEL, auto_install=auto_install)
