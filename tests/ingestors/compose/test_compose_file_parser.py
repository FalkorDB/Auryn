"""The compose YAML parser -> ParsedCompose. Pure, no graph."""

from __future__ import annotations

from auryn.ingestors.compose.file_parser import parse_source

_COMPOSE = (
    "services:\n"
    "  api:\n"
    "    build: ./api\n"
    "    image: shop/api:1.0\n"
    "    ports:\n"
    '      - "8080:8080"\n'
    "    depends_on:\n"
    "      - db\n"
    "  db:\n"
    "    image: postgres:16\n"
)


def test_services_parsed():
    p = parse_source(_COMPOSE)
    assert {s.name for s in p.services} == {"api", "db"}


def test_image_build_ports_depends():
    api = next(s for s in parse_source(_COMPOSE).services if s.name == "api")
    assert api.image == "shop/api:1.0"
    assert api.build_context == "./api" and api.build_dockerfile is None
    assert api.ports == ("8080:8080",)
    assert api.depends_on == ("db",)


def test_build_long_form():
    p = parse_source(
        "services:\n  web:\n    build:\n      context: ./web\n      dockerfile: Dockerfile.prod\n"
    )
    web = p.services[0]
    assert web.build_context == "./web" and web.build_dockerfile == "Dockerfile.prod"


def test_depends_on_long_form():
    p = parse_source(
        "services:\n  app:\n    image: x\n    depends_on:\n      db:\n        condition: service_healthy\n"
    )
    assert p.services[0].depends_on == ("db",)


def test_ports_long_form():
    p = parse_source(
        "services:\n  app:\n    image: x\n    ports:\n      - target: 8080\n        published: 80\n"
    )
    assert p.services[0].ports == ("80:8080",)


def test_non_compose_yaml_is_empty():
    # a YAML doc without a top-level `services:` map is not a compose file
    assert parse_source("name: my-app\nversion: 1\n").services == ()
    assert parse_source("- just\n- a\n- list\n").services == ()


def test_malformed_yaml_is_empty():
    assert parse_source("services: :: [not yaml").services == ()


def test_service_without_image_or_build():
    p = parse_source("services:\n  bare: {}\n")
    s = p.services[0]
    assert s.name == "bare" and s.image is None and s.build_context is None
