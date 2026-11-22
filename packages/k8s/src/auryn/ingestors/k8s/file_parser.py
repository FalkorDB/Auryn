"""Parse Kubernetes manifests -> `ParsedManifests` (PyYAML, multi-document). A file may hold several
`---`-separated docs (and `kind: List` bundles); each is a k8s object only if it has BOTH `apiVersion`
and `kind` -- the guard that lets us scan `*.yaml`/`*.yml` broadly yet ignore non-k8s YAML.

Every nested access is type-guarded: a manifest that is valid YAML but has a non-mapping where a
mapping is expected (`metadata: web`, `spec.template: x`, `containers: 5`, `backend: invalid`) must NOT
raise -- that object is skipped, and VALID sibling documents in the same file are still ingested."""

from __future__ import annotations

import yaml

from auryn.ingestors.k8s.model import IngressRoute, NetworkService, ParsedManifests, Workload

_WORKLOAD_KINDS = frozenset({
    "Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job", "CronJob", "Pod", "ReplicationController",
})
_DEFAULT_NS = "default"


def _map(x) -> dict:
    return x if isinstance(x, dict) else {}


def _list(x) -> list:
    return x if isinstance(x, list) else []


def _labels(meta: dict) -> tuple[tuple[str, str], ...]:
    labels = _map(meta.get("labels"))
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


def _pod_spec(kind: str, spec: dict) -> dict:
    """Where the container list lives per kind: Pod -> spec; CronJob -> jobTemplate.spec.template.spec;
    else spec.template.spec. Each hop is type-guarded."""
    if kind == "Pod":
        return spec
    if kind == "CronJob":
        return _map(_map(_map(_map(spec.get("jobTemplate")).get("spec")).get("template")).get("spec"))
    return _map(_map(spec.get("template")).get("spec"))


def _pod_template_meta(kind: str, spec: dict) -> dict:
    if kind == "Pod":
        return {}  # a Pod's own metadata.labels are read at the object level
    if kind == "CronJob":
        return _map(_map(_map(_map(spec.get("jobTemplate")).get("spec")).get("template")).get("metadata"))
    return _map(_map(spec.get("template")).get("metadata"))


def _images(kind: str, spec: dict) -> tuple[str, ...]:
    pod = _pod_spec(kind, spec)
    out: list[str] = []
    for key in ("initContainers", "containers"):
        for c in _list(pod.get(key)):
            if isinstance(c, dict) and c.get("image"):
                out.append(str(c["image"]))
    return tuple(out)


def _backend_name(backend) -> str | None:
    """networking.k8s.io/v1: `backend.service.name`; legacy extensions/v1beta1: `backend.serviceName`."""
    backend = _map(backend)
    svc = _map(backend.get("service"))
    if svc.get("name"):
        return str(svc["name"])
    if backend.get("serviceName"):
        return str(backend["serviceName"])
    return None


def _ingress_routes(name: str, namespace: str, spec: dict) -> list[IngressRoute]:
    routes: list[IngressRoute] = []
    default = spec.get("defaultBackend") or spec.get("backend")
    if isinstance(default, dict):
        routes.append(IngressRoute(name, namespace, None, "/", None, _backend_name(default), is_default=True))
    for rule in _list(spec.get("rules")):
        rule = _map(rule)
        host = rule.get("host")
        for p in _list(_map(rule.get("http")).get("paths")):
            p = _map(p)
            routes.append(IngressRoute(
                name, namespace, str(host) if host else None,
                str(p.get("path") or "/"), (str(p["pathType"]) if p.get("pathType") else None),
                _backend_name(p.get("backend")),
            ))
    return routes


def _iter_docs(data: str):
    """Yield each k8s object, unwrapping `kind: List` bundles (recursively)."""
    try:
        docs = list(yaml.safe_load_all(data))
    except yaml.YAMLError:
        return
    stack = list(docs)
    while stack:
        doc = stack.pop(0)
        if not isinstance(doc, dict):
            continue
        if str(doc.get("kind", "")).endswith("List") and isinstance(doc.get("items"), list):
            stack = doc["items"] + stack
            continue
        if "apiVersion" in doc and "kind" in doc:
            yield doc


def parse_source(data: str | bytes) -> ParsedManifests:
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="replace")

    workloads: list[Workload] = []
    services: list[NetworkService] = []
    ingresses: list[IngressRoute] = []
    for doc in _iter_docs(data):
        kind = str(doc["kind"])
        meta = _map(doc.get("metadata"))
        name = meta.get("name")
        if not name:
            continue
        name, namespace = str(name), str(meta.get("namespace") or _DEFAULT_NS)
        spec = _map(doc.get("spec"))
        if kind in _WORKLOAD_KINDS:
            labels = _labels(meta) if kind == "Pod" else _labels(_pod_template_meta(kind, spec))
            workloads.append(Workload(kind, name, namespace, _images(kind, spec), labels))
        elif kind == "Service":
            sel = tuple(sorted((str(k), str(v)) for k, v in _map(spec.get("selector")).items()))
            services.append(NetworkService(name, namespace, sel))
        elif kind == "Ingress":
            ingresses.extend(_ingress_routes(name, namespace, spec))
    return ParsedManifests(tuple(workloads), tuple(services), tuple(ingresses))
