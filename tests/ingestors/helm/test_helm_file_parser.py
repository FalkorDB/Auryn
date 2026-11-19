"""The Helm parser -> ParsedChart, and the values.yaml image/config extractors. Pure, no graph.

v1 is STATIC and render-free: only `Chart.yaml` + `values.yaml` are parsed (never the Go-templated
`templates/*.yaml`), so these tests cover chart metadata/dependencies and the values.yaml seams."""

from __future__ import annotations

from auryn.ingestors.helm.file_parser import load_values, parse_chart, parse_chart_yaml
from auryn.ingestors.helm.values import extract_config_values, extract_images

_CHART = """
apiVersion: v2
name: shop-api
version: 1.2.3
appVersion: "1.0"
type: application
dependencies:
  - name: redis
    version: "17.x"
    repository: https://charts.bitnami.com/bitnami
  - name: postgres
"""


# ── Chart.yaml ────────────────────────────────────────────────────────────────
def test_chart_yaml_metadata():
    c = parse_chart_yaml(_CHART)
    assert c.name == "shop-api"
    assert c.version == "1.2.3"
    assert c.app_version == "1.0"
    assert c.chart_type == "application"


def test_chart_yaml_dependencies_with_version_and_repository():
    c = parse_chart_yaml(_CHART)
    assert [(d.name, d.version, d.repository) for d in c.dependencies] == [
        ("redis", "17.x", "https://charts.bitnami.com/bitnami"),
        ("postgres", "", ""),
    ]


def test_chart_yaml_without_name_is_none():
    assert parse_chart_yaml("apiVersion: v2\nversion: 1.0\n") is None


def test_plain_k8s_manifest_is_not_a_chart():
    # a k8s manifest has `metadata.name`, not a top-level `name:` -> not a Chart.yaml
    manifest = "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\n"
    assert parse_chart_yaml(manifest) is None


def test_compose_file_is_not_a_chart():
    compose = "services:\n  web:\n    image: nginx:1.25\n"
    assert parse_chart_yaml(compose) is None


# ── values.yaml -> images ───────────────────────────────────────────────────────
def test_image_repository_and_tag():
    vals = load_values("image:\n  repository: shop/api\n  tag: \"1.0\"\n")
    assert extract_images(vals) == ("shop/api:1.0",)


def test_image_with_registry():
    vals = load_values("image:\n  registry: ghcr.io\n  repository: acme/api\n  tag: \"2\"\n")
    assert extract_images(vals) == ("ghcr.io/acme/api:2",)


def test_bare_image_string():
    vals = load_values('image: "ghcr.io/acme/api:1.2"\n')
    assert extract_images(vals) == ("ghcr.io/acme/api:1.2",)


def test_nested_component_image():
    vals = load_values(
        "worker:\n  image:\n    repository: shop/worker\n    tag: \"3\"\n"
        "primary:\n  image: redis:7\n"
    )
    assert set(extract_images(vals)) == {"shop/worker:3", "redis:7"}


def test_image_repository_without_tag():
    vals = load_values("image:\n  repository: shop/api\n")
    assert extract_images(vals) == ("shop/api",)  # core.image_ref applies the :latest default


def test_chart_with_no_images():
    vals = load_values("replicaCount: 2\nservice:\n  port: 80\n")
    assert extract_images(vals) == ()


def test_numeric_tag_is_stringified():
    vals = load_values("image:\n  repository: shop/api\n  tag: 1.0\n")  # unquoted -> YAML float
    assert extract_images(vals) == ("shop/api:1.0",)


# ── values.yaml -> config values ────────────────────────────────────────────────
def test_config_values_top_level_and_one_nested_level():
    vals = load_values(
        "replicaCount: 2\n"
        "service:\n  type: ClusterIP\n  port: 8080\n"
        "image:\n  repository: shop/api\n  tag: \"1.0\"\n"
    )
    cvs = dict(extract_config_values(vals))
    assert cvs["replicaCount"] == "2"
    assert cvs["service.type"] == "ClusterIP"
    assert cvs["service.port"] == "8080"
    assert cvs["image.repository"] == "shop/api"  # image-related keys are config values too


def test_config_values_are_bounded_in_depth():
    # a depth-3 leaf (`resources.limits.cpu`) is NOT emitted -- kept sensible, not every leaf
    vals = load_values("resources:\n  limits:\n    cpu: \"500m\"\n")
    names = [n for n, _ in extract_config_values(vals)]
    assert "resources.limits.cpu" not in names


def test_bool_config_value_lowercased():
    vals = load_values("ingress:\n  enabled: true\n")
    assert dict(extract_config_values(vals))["ingress.enabled"] == "true"


# ── malformed / empty ───────────────────────────────────────────────────────────
def test_malformed_values_handled_gracefully():
    assert load_values("this: : : not valid") == {}
    assert extract_images(load_values("]{[ broken")) == ()
    assert extract_config_values(load_values("]{[ broken")) == ()


def test_parse_chart_assembles_images_and_config():
    pc = parse_chart("charts/shop", _CHART, "image:\n  repository: shop/api\n  tag: \"1.0\"\nreplicaCount: 2\n")
    assert pc.chart.name == "shop-api"
    assert pc.images == ("shop/api:1.0",)
    assert ("replicaCount", "2") in pc.config_values
    assert pc.has_values is True


def test_parse_chart_without_values():
    pc = parse_chart("charts/shop", _CHART, None)
    assert pc.images == ()
    assert pc.config_values == ()
    assert pc.has_values is False


def test_implausible_image_values_are_skipped():
    # prose, an empty tag, or a templated tag on an `image:` key must NOT become an Image ref
    assert extract_images(load_values('image: "see the deployment docs"\n')) == ()
    assert extract_images(load_values('image: "shop/api:"\n')) == ()
    assert extract_images(load_values('image:\n  repository: shop/api\n  tag: "{{ .Chart.AppVersion }}"\n')) == ()
    # ... but a real ref still passes (incl. a registry:port host, whose `:` is not a tag)
    assert extract_images(load_values('image: "shop/api:1.0"\n')) == ("shop/api:1.0",)
    assert extract_images(load_values('image: "localhost:5000/shop/api:1.0"\n')) == ("localhost:5000/shop/api:1.0",)


def test_non_list_dependencies_is_ignored_not_fatal():
    # a malformed `dependencies: 42` must not raise (which would drop the whole chart) -- just no deps
    c = parse_chart_yaml("apiVersion: v2\nname: shop\nversion: 1.0.0\ndependencies: 42\n")
    assert c is not None and c.name == "shop" and c.dependencies == ()


def test_sensitive_key_detection():
    from auryn.ingestors.helm.build import _is_sensitive
    for s in ("database.password", "api.key", "tls.key", "auth.token", "signing_key", "auth.passphrase",
              "auth.pwd", "apiKey", "my.secret", "aws.access_key", "db.credential", "keys"):
        assert _is_sensitive(s), s
    for s in ("replicaCount", "image.repository", "monkey", "keyboard", "service.port", "publicKey"):
        assert not _is_sensitive(s), s


def test_url_credential_redaction():
    from auryn.ingestors.helm.build import _redact_value
    assert _redact_value("postgres://user:s3cr3t@host:5432/db") == "postgres://user:[redacted]@host:5432/db"
    assert _redact_value("redis://:p4ss@cache") == "redis://:[redacted]@cache"
    # no credentials -> untouched
    assert _redact_value("https://example.com/path") == "https://example.com/path"
    assert _redact_value("just a plain value") == "just a plain value"
