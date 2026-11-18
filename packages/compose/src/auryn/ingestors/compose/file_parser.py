"""Parse a docker-compose file -> `ParsedCompose` (PyYAML). Only the fields the deployment graph needs
-- services, their `image:`, `build:`, `depends_on:`, `ports:`. A YAML document without a top-level
`services:` map is not a compose file and yields nothing (the suffix match is broad, so this guard
keeps a stray `*.compose.yml` from producing junk)."""

from __future__ import annotations

import yaml

from auryn.ingestors.compose.model import ParsedCompose, Service


def _as_str(v) -> str | None:
    return str(v) if v is not None else None


def _depends_names(depends) -> tuple[str, ...]:
    """`depends_on` is a list (`[db, queue]`) or the long-form map (`{db: {condition: ...}}`)."""
    if isinstance(depends, list):
        return tuple(str(d) for d in depends)
    if isinstance(depends, dict):
        return tuple(str(k) for k in depends)
    return ()


def _ports(ports) -> tuple[str, ...]:
    """`ports` is a list of `"8080:8080"` strings/ints, or long-form `{target, published, protocol,
    host_ip}` maps -- normalized so the long form carries the same host/protocol detail the short form
    does (so `{target:53, published:53, protocol:udp}` isn't confused with the TCP port)."""
    out: list[str] = []
    if isinstance(ports, list):
        for p in ports:
            if isinstance(p, dict):
                tgt, pub, proto, host = p.get("target"), p.get("published"), p.get("protocol"), p.get("host_ip")
                s = f"{pub}:{tgt}" if pub is not None else str(tgt)
                if host:
                    s = f"{host}:{s}"
                if proto:
                    s = f"{s}/{proto}"
                out.append(s)
            else:
                out.append(str(p))
    return tuple(out)


def _build(spec) -> tuple[str | None, str | None, str | None]:
    """`build:` is a context string (`./api`) or a map (`{context, dockerfile, target}`). A map defaults
    its context to `.` (Compose's own default) -- except a `dockerfile_inline` build, which has no file
    on disk to resolve, so it stays unbuilt here."""
    build = spec.get("build")
    if isinstance(build, str):
        return build, None, None
    if isinstance(build, dict):
        if build.get("dockerfile_inline") is not None:
            return None, None, None
        return _as_str(build.get("context", ".")), _as_str(build.get("dockerfile")), _as_str(build.get("target"))
    return None, None, None


def parse_source(data: str | bytes) -> ParsedCompose:
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="replace")
    try:
        doc = yaml.safe_load(data)
    except yaml.YAMLError:
        return ParsedCompose()
    if not isinstance(doc, dict) or not isinstance(doc.get("services"), dict):
        return ParsedCompose()  # not a compose file

    services: list[Service] = []
    for name, spec in doc["services"].items():
        if not isinstance(spec, dict):
            spec = {}
        ctx, dockerfile, target = _build(spec)
        services.append(
            Service(
                name=str(name),
                image=_as_str(spec.get("image")),
                build_context=ctx,
                build_dockerfile=dockerfile,
                build_target=target,
                depends_on=_depends_names(spec.get("depends_on")),
                ports=_ports(spec.get("ports")),
            )
        )
    return ParsedCompose(tuple(services))
