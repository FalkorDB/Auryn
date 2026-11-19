"""The Kustomize parser -> ParsedKustomization, and the `images:` transformer resolution. Pure, no graph."""

from __future__ import annotations

from auryn.ingestors.kustomize.file_parser import parse_kustomization


def _p(data, d="overlays/prod"):
    return parse_kustomization(d, data)


# ── images: transformer ────────────────────────────────────────────────────────
def test_newname_and_newtag():
    p = _p("images:\n  - name: nginx\n    newName: myreg.io/nginx\n    newTag: \"1.2\"\n")
    assert p.valid and p.images == ("myreg.io/nginx:1.2",)


def test_newtag_only_keeps_matched_name():
    p = _p("images:\n  - name: nginx\n    newTag: \"1.21\"\n")
    assert p.images == ("nginx:1.21",)


def test_digest_overrides_tag():
    p = _p("images:\n  - name: nginx\n    digest: sha256:abc123\n")
    assert p.images == ("nginx@sha256:abc123",)


def test_newname_only_without_tag_is_skipped():
    # repo replaced but no tag anywhere -> the base's tag is unknown render-free; don't guess `:latest`
    # (a false hub join), so skip it
    p = _p("images:\n  - name: nginx\n    newName: myreg.io/nginx\n")
    assert p.images == ()


def test_newname_keeps_the_matched_name_tag():
    # a tagged match target supplies the tag when the repo is renamed (Kustomize keeps it)
    p = _p("images:\n  - name: nginx:1.27\n    newName: myreg.io/nginx\n")
    assert p.images == ("myreg.io/nginx:1.27",)


def test_newtag_strips_the_tag_from_name():
    # `name: nginx:1.19` + `newTag: 1.21` must NOT become `nginx:1.19:1.21`
    assert _p("images:\n  - name: nginx:1.19\n    newTag: \"1.21\"\n").images == ("nginx:1.21",)


def test_non_list_images_and_resources_are_not_fatal():
    # `images: 42` / `resources: true` must not raise (which would abort the whole sync)
    p = _p("images: 42\nresources: true\n")
    assert p.valid and p.images == () and p.bases == ()


def test_bare_name_is_a_noop_and_skipped():
    # a match target with no override asserts nothing concrete -> not an image
    p = _p("images:\n  - name: nginx\n")
    assert p.images == ()


def test_multiple_images_deduped_in_order():
    p = _p(
        "images:\n"
        "  - name: a\n    newName: reg/a\n    newTag: \"1\"\n"
        "  - name: b\n    newTag: \"2\"\n"
        "  - name: a\n    newName: reg/a\n    newTag: \"1\"\n"
    )
    assert p.images == ("reg/a:1", "b:2")


def test_implausible_image_is_skipped():
    p = _p("images:\n  - name: x\n    newName: \"see the docs\"\n    newTag: \"1\"\n")
    assert p.images == ()


# ── bases / resources ──────────────────────────────────────────────────────────
def test_bases_from_resources_bases_components():
    p = _p(
        "resources:\n  - ../../base\n  - ./extra.yaml\n"
        "bases:\n  - ../legacy\n"
        "components:\n  - ../../components/logging\n"
    )
    assert p.bases == ("../../base", "./extra.yaml", "../legacy", "../../components/logging")


# ── validity ───────────────────────────────────────────────────────────────────
def test_kind_kustomization_is_valid():
    p = _p("apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nnamespace: prod\n")
    assert p.valid and p.namespace == "prod"


def test_kind_omitted_is_still_valid():
    # kustomize recognizes a kustomization.yaml by basename even without an explicit kind
    assert _p("resources:\n  - ./a.yaml\n").valid is True


def test_wrong_kind_is_invalid():
    # a manifest that merely shares the name but declares another kind is not a kustomization
    p = _p("apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\n")
    assert p.valid is False and p.images == ()


def test_malformed_yaml_is_invalid():
    assert _p("this: : : not valid").valid is False
