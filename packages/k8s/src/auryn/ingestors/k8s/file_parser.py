"""Parse Kubernetes manifests -> `ParsedManifests` (PyYAML, multi-document). A manifest file may hold
several `---`-separated docs; each is a k8s object only if it has BOTH `apiVersion` and `kind` (the
guard that lets us scan `*.yaml`/`*.yml` broadly yet ignore non-k8s YAML -- compose, CI configs, ...).

Only the fields the deployment graph needs: a workload's container images, and an Ingress's host/path
rules + backend service."""

from __future__ import annotations

import yaml

from auryn.ingestors.k8s.model import IngressRoute, ParsedManifests, Workload

# Kinds whose pod template runs container images. (Pod has containers directly; the rest nest them
# under spec.template.spec.)
_WORKLOAD_KINDS = frozenset({
    "Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job", "CronJob", "Pod", "ReplicationController",
})
_DEFAULT_NS = "default"


def _pod_spec(kind: str, spec: dict) -> dict:
    """Where the container list lives for each kind: Pod -> spec; CronJob -> jobTemplate.spec.template
    .spec; everything else -> spec.template.spec."""
    if kind == "Pod":
        return spec
    if kind == "CronJob":
        return (((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}).get("spec") or {}
    return (spec.get("template") or {}).get("spec") or {}


def _images(kind: str, spec: dict) -> tuple[str, ...]:
    pod = _pod_spec(kind, spec)
    out: list[str] = []
    for key in ("initContainers", "containers"):
        for c in pod.get(key) or []:
            if isinstance(c, dict) and c.get("image"):
                out.append(str(c["image"]))
    return tuple(out)


def _ingress_routes(namespace: str, spec: dict) -> list[IngressRoute]:
    routes: list[IngressRoute] = []
    # default backend (no host/path)
    default = spec.get("defaultBackend") or spec.get("backend")
    if isinstance(default, dict):
        routes.append(IngressRoute(namespace, None, "/", _backend_name(default)))
    for rule in spec.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        host = rule.get("host")
        for p in ((rule.get("http") or {}).get("paths") or []):
            if isinstance(p, dict):
                routes.append(IngressRoute(namespace, host, str(p.get("path") or "/"), _backend_name(p.get("backend") or {})))
    return routes


def _backend_name(backend: dict) -> str | None:
    """networking.k8s.io/v1: `backend.service.name`; legacy extensions/v1beta1: `backend.serviceName`."""
    svc = backend.get("service")
    if isinstance(svc, dict) and svc.get("name"):
        return str(svc["name"])
    if backend.get("serviceName"):
        return str(backend["serviceName"])
    return None


def parse_source(data: str | bytes) -> ParsedManifests:
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="replace")
    try:
        docs = list(yaml.safe_load_all(data))
    except yaml.YAMLError:
        return ParsedManifests()

    workloads: list[Workload] = []
    ingresses: list[IngressRoute] = []
    for doc in docs:
        if not isinstance(doc, dict) or "apiVersion" not in doc or "kind" not in doc:
            continue  # not a k8s object
        kind = str(doc["kind"])
        meta = doc.get("metadata") or {}
        name = meta.get("name")
        if not name:
            continue
        namespace = str(meta.get("namespace") or _DEFAULT_NS)
        spec = doc.get("spec") or {}
        if not isinstance(spec, dict):
            spec = {}
        if kind in _WORKLOAD_KINDS:
            workloads.append(Workload(kind=kind, name=str(name), namespace=namespace, images=_images(kind, spec)))
        elif kind == "Ingress":
            ingresses.extend(_ingress_routes(namespace, spec))
    return ParsedManifests(tuple(workloads), tuple(ingresses))
