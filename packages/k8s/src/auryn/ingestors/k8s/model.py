"""Parsed Kubernetes manifest model (engine-agnostic). `file_parser.parse_source` produces these;
`build.build_graph` turns them into graph nodes/edges."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Workload:
    """A runnable k8s object (Deployment / StatefulSet / DaemonSet / Job / CronJob / Pod / ...). Its
    pod template's container images are what it RUNS_IMAGE on the shared hub. `labels` are the pod
    template's labels -- what a networking Service's selector matches to route to this workload."""

    kind: str
    name: str
    namespace: str
    images: tuple[str, ...] = ()
    labels: tuple[tuple[str, str], ...] = ()  # pod-template labels, as sorted (k, v) pairs


@dataclass(frozen=True)
class NetworkService:
    """A k8s networking `Service` -- the object an Ingress backend names. Its `selector` picks the
    workload(s) it fronts, so the stitch resolves `Ingress -> Service -> (selector) -> Workload`."""

    name: str
    namespace: str
    selector: tuple[tuple[str, str], ...] = ()  # sorted (k, v) label pairs


@dataclass(frozen=True)
class IngressRoute:
    """One host+path rule of an Ingress (or its default backend), and the k8s Service name it routes
    to. `ingress_name` keeps two Ingresses in one namespace that share a host/path distinct; `is_default`
    marks the catch-all default backend (no host/path)."""

    ingress_name: str
    namespace: str
    host: str | None
    path: str
    path_type: str | None
    backend_service: str | None
    is_default: bool = False


@dataclass(frozen=True)
class ParsedManifests:
    workloads: tuple[Workload, ...] = ()
    services: tuple[NetworkService, ...] = ()
    ingresses: tuple[IngressRoute, ...] = ()
