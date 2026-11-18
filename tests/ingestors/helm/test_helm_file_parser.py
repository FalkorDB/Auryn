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
