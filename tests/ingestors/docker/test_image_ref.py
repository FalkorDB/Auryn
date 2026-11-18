"""Image-reference normalization (ADR 0007 §2) -- the join key the whole deployment tier agrees on.
Pure functions, no graph."""

from __future__ import annotations

import pytest

from auryn.ingestors.docker.image_ref import image_id, normalize_image_ref, parse_image_ref


@pytest.mark.parametrize(
    "ref, expected",
    [
        ("nginx", "docker.io/library/nginx:latest"),                       # official single-name + defaults
        ("python:3.12-slim", "docker.io/library/python:3.12-slim"),        # official + explicit tag
        ("acme/api", "docker.io/acme/api:latest"),                         # user namespace, no library/
        ("ghcr.io/acme/api:1.2", "ghcr.io/acme/api:1.2"),                  # explicit registry
        ("GHCR.io/Acme/API:1.2", "ghcr.io/acme/api:1.2"),                  # registry+repo lowered, tag kept-case...
        ("localhost:5000/app", "localhost:5000/app:latest"),              # registry port is not a tag
        ("registry.io:443/team/svc:v2", "registry.io:443/team/svc:v2"),   # host port + tag together
    ],
)
def test_normalize(ref, expected):
    assert normalize_image_ref(ref) == expected


def test_tag_is_case_sensitive():
    # The registry/repo fold to lowercase, but a tag is case-sensitive and must survive verbatim.
    assert normalize_image_ref("ghcr.io/Acme/API:Release-RC1") == "ghcr.io/acme/api:Release-RC1"


def test_digest_wins_over_tag():
    r = parse_image_ref("localhost:5000/app:ignored@sha256:deadbeef")
    assert r.digest == "sha256:deadbeef"
    assert normalize_image_ref("localhost:5000/app:ignored@sha256:deadbeef") == "localhost:5000/app@sha256:deadbeef"


def test_image_id_prefix():
    assert image_id("nginx") == "image://docker.io/library/nginx:latest"


def test_unresolved_variable_flagged():
    r = parse_image_ref("${BASE_IMAGE}:${TAG}")
    assert r.unresolved is True
    assert r.raw == "${BASE_IMAGE}:${TAG}"


def test_same_ref_different_spelling_same_id():
    # The contract: two spellings of the same image collapse to one id (the hub-merge guarantee).
    assert image_id("nginx") == image_id("docker.io/library/nginx:latest")
    assert image_id("python:3.12-slim") == image_id("docker.io/library/python:3.12-slim")
