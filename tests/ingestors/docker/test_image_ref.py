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


def test_unresolved_variable_name_case_preserved():
    # An unresolved ref must NOT case-fold the variable name (`${BASE_IMAGE}` != `${base_image}`), or
    # the placeholder can't round-trip to the raw text a later substitution expands.
    assert "BASE_IMAGE" in image_id("${BASE_IMAGE}")
    assert "base_image" not in image_id("${BASE_IMAGE}")


def test_unresolved_refs_keep_raw_identity_and_dont_merge():
    # `$BASE` and `docker.io/library/$BASE` expand to different repos, so they must NOT collapse to one
    # id -- an unresolved ref is keyed by its RAW text, not a normalized form.
    assert image_id("$BASE") == "image://$BASE"
    assert image_id("docker.io/library/$BASE") == "image://docker.io/library/$BASE"
    assert image_id("$BASE") != image_id("docker.io/library/$BASE")


def test_file_id_preserves_leading_dot_dir():
    from auryn.ingestors.docker.ids import file_id, project_id

    proj = project_id("demo")
    assert file_id(proj, ".github/Dockerfile") == f"{proj}/file/.github/Dockerfile"   # dot kept
    assert file_id(proj, "./app/Dockerfile") == f"{proj}/file/app/Dockerfile"          # only ./ stripped


def test_same_ref_different_spelling_same_id():
    # The contract: two spellings of the same image collapse to one id (the hub-merge guarantee).
    assert image_id("nginx") == image_id("docker.io/library/nginx:latest")
    assert image_id("python:3.12-slim") == image_id("docker.io/library/python:3.12-slim")


def test_docker_hub_host_and_case_variants_collapse():
    # Docker Hub's legacy hosts + a shouted registry case all key the SAME node as the bare name --
    # the library/ namespace must still be applied after the host is folded/lowercased.
    canonical = image_id("docker.io/library/nginx:latest")
    for ref in ("nginx", "docker.io/nginx", "DOCKER.IO/nginx",
                "index.docker.io/nginx", "registry-1.docker.io/library/nginx"):
        assert image_id(ref) == canonical, ref
