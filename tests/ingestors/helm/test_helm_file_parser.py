"""The Helm parser -> ParsedChart, and the values->image resolver. Pure, no graph."""

from __future__ import annotations

from auryn.ingestors.helm.file_parser import parse_chart, parse_chart_yaml
from auryn.ingestors.helm.values import lookup, resolve_image

_CHART = """
apiVersion: v2
name: shop-api
version: 1.2.3
appVersion: "1.0"
dependencies:
  - name: redis
    version: "17.x"
    repository: https://charts.bitnami.com/bitnami
  - name: postgres
"""

_VALUES = """
image:
  repository: shop/api
  tag: "1.0"
replicaCount: 2
"""

_DEPLOYMENT = """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: shop-api
spec:
  replicas: {{ .Values.replicaCount }}
  template:
    spec:
      containers:
        - name: api
          image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
"""


def _chart(templates):
    return parse_chart("charts/shop", _CHART, _VALUES, templates)


# ── Chart.yaml ────────────────────────────────────────────────────────────────
def test_chart_yaml_metadata_and_deps():
    c = parse_chart_yaml(_CHART)
    assert c.name == "shop-api"
    assert c.version == "1.2.3"
    assert c.app_version == "1.0"
    assert c.dependencies == ("redis", "postgres")


def test_chart_yaml_without_name_is_none():
    assert parse_chart_yaml("apiVersion: v2\nversion: 1.0\n") is None


def test_non_list_dependencies_is_ignored_not_fatal():
    # a malformed `dependencies: true` must not raise -- the chart metadata is still parsed, deps empty
    c = parse_chart_yaml("apiVersion: v2\nname: x\nversion: 1.0\ndependencies: true\n")
    assert c is not None and c.name == "x" and c.dependencies == ()


# ── values resolution ─────────────────────────────────────────────────────────
def test_lookup_dotted_path():
    assert lookup({"image": {"repository": "shop/api"}}, "image.repository") == "shop/api"
    assert lookup({"image": {}}, "image.tag") is None


def test_resolve_image_from_values():
    vals = {"image": {"repository": "shop/api", "tag": "1.0"}}
    assert resolve_image('"{{ .Values.image.repository }}:{{ .Values.image.tag }}"', vals) == "shop/api:1.0"


def test_resolve_literal_image_passes_through():
    assert resolve_image("nginx:1.25", {}) == "nginx:1.25"


def test_resolve_unresolvable_template_is_none():
    # a non-.Values action (`.Release.Name`) can't be resolved render-free -> not materialized
    assert resolve_image('"{{ .Release.Name }}-api:{{ .Values.tag }}"', {"tag": "1.0"}) is None


def test_resolve_missing_value_is_none():
    assert resolve_image("{{ .Values.image.repository }}:{{ .Values.image.tag }}", {"image": {}}) is None


# ── templates -> workloads ────────────────────────────────────────────────────
def test_deployment_workload_with_resolved_image():
    pc = _chart([("templates/deployment.yaml", _DEPLOYMENT)])
    assert len(pc.workloads) == 1
    w = pc.workloads[0]
    assert w.kind == "Deployment"
    assert w.name == "shop-api"             # from metadata.name
    assert w.images == ("shop/api:1.0",)    # resolved from values


def test_templated_name_falls_back_to_template_stem():
    tmpl = (
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n"
        '  name: {{ include "shop.fullname" . }}\nspec:\n  template:\n    spec:\n'
        '      containers:\n        - image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"\n'
    )
    pc = _chart([("templates/worker.yaml", tmpl)])
    assert pc.workloads[0].name == "worker"   # the template file stem


def test_non_workload_kind_yields_no_workload():
    svc = "apiVersion: v1\nkind: Service\nmetadata:\n  name: shop-api\n"
    assert _chart([("templates/service.yaml", svc)]).workloads == ()


def test_multi_doc_template_finds_the_workload():
    multi = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: cm\n---\n" + _DEPLOYMENT
    pc = _chart([("templates/all.yaml", multi)])
    assert [w.kind for w in pc.workloads] == ["Deployment"]


def test_unresolvable_image_is_not_materialized():
    # the tag comes from `.Chart.AppVersion` (not resolvable render-free) -> no concrete ref, no image
    tmpl = (
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: x\nspec:\n  template:\n    spec:\n"
        '      containers:\n        - image: "{{ .Values.image.repository }}:{{ .Chart.AppVersion }}"\n'
    )
    pc = _chart([("templates/x.yaml", tmpl)])
    assert pc.workloads[0].images == ()


# ── image resolution hardening (render-free edge cases) ─────────────────────────
def test_resolve_pipeline_is_not_evaluated():
    # a pipeline/function on a .Values ref can't be evaluated render-free -> NOT materialized (taking the
    # raw value would be wrong: `{{ .Values.tag | upper }}` is not `.Values.tag`)
    assert resolve_image("{{ .Values.repo }}:{{ .Values.tag | upper }}", {"repo": "shop/api", "tag": "1.0"}) is None


def test_resolve_shell_var_rejected():
    assert resolve_image("$IMAGE", {}) is None
    assert resolve_image("${IMAGE}:latest", {}) is None


def test_resolve_empty_value_rejected():
    # an empty tag renders `shop/api:`, which core.image_ref silently forces to `:latest` -> a false edge
    assert resolve_image("{{ .Values.repo }}:{{ .Values.tag }}", {"repo": "shop/api", "tag": ""}) is None


def test_resolve_malformed_trailing_colon_rejected():
    assert resolve_image("shop/api:", {}) is None


def test_resolve_prose_value_rejected():
    assert resolve_image("see the deployment docs", {}) is None  # whitespace -> not an image ref


# ── templates: inline comments, over-match, within-file disambiguation ───────────
def test_inline_comment_stripped_from_image():
    tmpl = (
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: api\nspec:\n  template:\n    spec:\n"
        "      containers:\n        - image: nginx:1.25  # pinned for prod\n"
    )
    assert _chart([("templates/d.yaml", tmpl)]).workloads[0].images == ("nginx:1.25",)


def test_prose_image_key_not_materialized():
    tmpl = (
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: api\nspec:\n  template:\n    spec:\n"
        "      containers:\n        - image: the base image to use\n"
    )
    assert _chart([("templates/d.yaml", tmpl)]).workloads[0].images == ()


def test_two_templated_workloads_in_one_file_are_disambiguated():
    doc = (
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n"
        '  name: {{ include "x.fullname" . }}\nspec:\n  template:\n    spec:\n      containers:\n        - image: a:1\n'
    )
    pc = _chart([("templates/multi.yaml", doc + "---\n" + doc)])
    assert [w.name for w in pc.workloads] == ["multi", "multi-2"]  # both fall back to the stem -> suffixed


def test_template_path_is_chart_relative_from_repo_path():
    # given a full repo path, Workload.template is the path RELATIVE to the chart dir (part of the id)
    pc = parse_chart("charts/shop", _CHART, _VALUES, [("charts/shop/templates/nested/deploy.yaml", _DEPLOYMENT)])
    assert pc.workloads[0].template == "templates/nested/deploy.yaml"


def test_document_separator_with_trailing_comment_is_not_swallowed():
    doc = (
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: a\nspec:\n  template:\n    spec:\n"
        "      containers:\n        - image: a:1\n"
    )
    multi = doc + "--- # Source: shop/templates/b.yaml\n" + doc.replace("name: a", "name: b")
    pc = _chart([("templates/all.yaml", multi)])
    assert sorted(w.name for w in pc.workloads) == ["a", "b"]  # `--- # ...` still splits the documents


def test_metadata_name_inline_comment_stripped():
    tmpl = (
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: api  # prod\nspec:\n  template:\n"
        "    spec:\n      containers:\n        - image: a:1\n"
    )
    assert _chart([("templates/d.yaml", tmpl)]).workloads[0].name == "api"
