"""Parsed Kubernetes manifest model (engine-agnostic). `file_parser.parse_source` produces these;
`build.build_graph` turns them into graph nodes/edges."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Workload:
    """A runnable k8s object (Deployment / StatefulSet / DaemonSet / Job / CronJob / Pod / ...). Its
    pod template's container images are what it RUNS_IMAGE on the shared hub."""

    kind: str
    name: str
    namespace: str
    images: tuple[str, ...] = ()


@dataclass(frozen=True)
class IngressRoute:
    """One host+path rule of an Ingress, and the k8s Service name it routes to. The stitch keys a
    route-keyed APIEndpoint (shared with the HTTP seam) and links the backing workload to it."""

    namespace: str
    host: str | None
    path: str
    backend_service: str | None


@dataclass(frozen=True)
class ParsedManifests:
    workloads: tuple[Workload, ...] = ()
    ingresses: tuple[IngressRoute, ...] = field(default_factory=tuple)
