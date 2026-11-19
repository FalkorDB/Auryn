"""The k8s manifest parser -> ParsedManifests. Pure, no graph."""

from __future__ import annotations

from auryn.ingestors.k8s.file_parser import parse_source

_DEPLOY = (
    "apiVersion: apps/v1\n"
    "kind: Deployment\n"
    "metadata: {name: api, namespace: shop}\n"
    "spec:\n"
    "  template:\n"
    "    spec:\n"
    "      initContainers:\n"
    "        - {name: migrate, image: shop/migrate:1}\n"
    "      containers:\n"
    "        - {name: api, image: shop/api:1.0}\n"
)


def test_deployment_images():
    w = parse_source(_DEPLOY).workloads[0]
    assert w.kind == "Deployment" and w.name == "api" and w.namespace == "shop"
    assert w.images == ("shop/migrate:1", "shop/api:1.0")  # init + main containers


def test_pod_containers_direct():
    w = parse_source(
        "apiVersion: v1\nkind: Pod\nmetadata: {name: p}\nspec:\n  containers:\n    - {name: c, image: nginx}\n"
    ).workloads[0]
    assert w.kind == "Pod" and w.images == ("nginx",) and w.namespace == "default"  # default ns


def test_cronjob_nested_template():
    w = parse_source(
        "apiVersion: batch/v1\nkind: CronJob\nmetadata: {name: cj}\n"
        "spec:\n  jobTemplate:\n    spec:\n      template:\n        spec:\n          containers:\n            - {name: c, image: busybox}\n"
    ).workloads[0]
    assert w.kind == "CronJob" and w.images == ("busybox",)


def test_ingress_v1_backend():
    m = parse_source(
        "apiVersion: networking.k8s.io/v1\nkind: Ingress\nmetadata: {name: ing, namespace: shop}\n"
        "spec:\n  rules:\n    - host: a.example.com\n      http:\n        paths:\n"
        "          - path: /api\n            backend: {service: {name: api-svc}}\n"
    )
    assert len(m.ingresses) == 1
    r = m.ingresses[0]
    assert r.host == "a.example.com" and r.path == "/api" and r.backend_service == "api-svc" and r.namespace == "shop"


def test_ingress_legacy_backend():
    r = parse_source(
        "apiVersion: extensions/v1beta1\nkind: Ingress\nmetadata: {name: ing}\n"
        "spec:\n  rules:\n    - http:\n        paths:\n          - path: /x\n            backend: {serviceName: legacy}\n"
    ).ingresses[0]
    assert r.backend_service == "legacy" and r.host is None


def test_multi_doc():
    m = parse_source(_DEPLOY + "---\n" + "apiVersion: v1\nkind: Pod\nmetadata: {name: p}\nspec:\n  containers:\n    - {name: c, image: x}\n")
    assert len(m.workloads) == 2


def test_non_k8s_yaml_ignored():
    # no apiVersion/kind -> not a manifest (so scanning every *.yaml is safe)
    assert parse_source("services:\n  api:\n    image: x\n").workloads == ()
    assert parse_source("name: ci\njobs: {build: {}}\n").workloads == ()


def test_malformed_yaml_is_empty():
    m = parse_source("apiVersion: v1\nkind: : bad")
    assert m.workloads == () and m.ingresses == ()


def test_non_mapping_nested_values_dont_crash_and_siblings_survive():
    # a manifest with a non-mapping where a mapping is expected must be skipped, NOT raise -- and a
    # valid sibling doc in the same file is still parsed.
    src = (
        "apiVersion: apps/v1\nkind: Deployment\nmetadata: web\nspec:\n  template: nope\n"  # all junk
        "---\n"
        "apiVersion: v1\nkind: Pod\nmetadata: {name: ok}\nspec:\n  containers: 7\n"        # containers not a list
        "---\n"
        "apiVersion: v1\nkind: Pod\nmetadata: {name: good}\nspec:\n  containers:\n    - {name: c, image: x}\n"
    )
    m = parse_source(src)  # must not raise
    names = sorted(w.name for w in m.workloads)
    assert "good" in names  # the valid sibling survived
    good = next(w for w in m.workloads if w.name == "good")
    assert good.images == ("x",)


def test_kind_list_is_unwrapped():
    src = (
        "apiVersion: v1\nkind: List\nitems:\n"
        "  - apiVersion: apps/v1\n    kind: Deployment\n    metadata: {name: a}\n"
        "    spec: {template: {spec: {containers: [{name: c, image: i1}]}}}\n"
        "  - apiVersion: v1\n    kind: Pod\n    metadata: {name: b}\n    spec: {containers: [{name: c, image: i2}]}\n"
    )
    m = parse_source(src)
    assert sorted(w.name for w in m.workloads) == ["a", "b"]


def test_service_selector_and_pod_labels():
    m = parse_source(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: api}\n"
        "spec:\n  template:\n    metadata:\n      labels: {app: api, tier: web}\n"
        "    spec:\n      containers:\n        - {name: c, image: x}\n"
        "---\n"
        "apiVersion: v1\nkind: Service\nmetadata: {name: api-svc}\nspec:\n  selector: {app: api}\n"
    )
    assert m.workloads[0].labels == (("app", "api"), ("tier", "web"))
    assert m.services[0].name == "api-svc" and m.services[0].selector == (("app", "api"),)
