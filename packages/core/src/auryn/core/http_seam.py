"""The cross-ingestor HTTP-seam contract (ADR 0006): how an outbound HTTP call is keyed so every
caller -- in any language -- converges on ONE route-keyed APIEndpoint placeholder node, which the
`http-endpoints` stitch hook then resolves to the handler.

Both the route normalization and the key MUST live here (not per ingestor): if the C# and TS
callers normalized routes differently, a JS `fetch("/api/x")` and a C# `GetAsync("/api/x")` would
key to different placeholders and never converge. Keep this the single source of truth.
"""

from __future__ import annotations


def normalize_route(url: str) -> str:
    """Canonical route for keying: drop query/fragment, strip a trailing slash, lowercase, and root
    a bare path with a leading slash. Template params (`{id}`) are left as-is for the stitch hook's
    matcher.

    The leading-slash canonicalization is what lets a *caller* and an *endpoint declaration* land on
    ONE node: a controller's `[HttpGet("api/x")]` route is stored relative (`api/x`) while a caller
    writes `GetAsync("/api/x")` -- rooting both to `/api/x` gives them the same route-keyed id, so
    the two merge instead of duplicating. Absolute URLs (`http(s)://host/...`) are left as-is."""
    route = url.split("?", 1)[0].split("#", 1)[0].strip()
    if len(route) > 1:
        route = route.rstrip("/")
    route = route.lower()
    if route and not route.startswith(("/", "http://", "https://")):
        route = "/" + route
    return route


def endpoint_placeholder_key(http_method: str, route: str) -> str:
    """Language-neutral id for the shared route-keyed APIEndpoint placeholder. Matched by the
    stitch hook on route+verb, not by the per-language symbol linkage."""
    return f"extern://http/{http_method}:{route}"


def looks_like_route(url: str) -> bool:
    """A captured string is plausibly an HTTP route (has a path or scheme) rather than an
    arbitrary key/argument -- used to keep name-based caller heuristics (esp. C#'s typeless
    tree-sitter pass) from mistaking `repo.GetAsync("key")` for an HTTP call."""
    u = url.strip()
    return u.startswith(("/", "http://", "https://")) or "/" in u
