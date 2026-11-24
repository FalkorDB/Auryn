"""The hand-rolled Dockerfile parser -> ParsedDockerfile. Pure, no graph."""

from __future__ import annotations

from auryn.ingestors.docker.file_parser import parse_source

_MULTISTAGE = (
    "# syntax=docker/dockerfile:1\n"
    "ARG PYTHON_VERSION=3.12-slim\n"
    "\n"
    "FROM python:${PYTHON_VERSION} AS base\n"
    "WORKDIR /app\n"
    "COPY requirements.txt .\n"
    "RUN pip install -r requirements.txt   # a trailing comment, not a line\n"
    "\n"
    "FROM base AS final\n"
    "COPY ./app /app/app\n"
    "COPY --from=base /usr/local/lib /usr/local/lib\n"
    "EXPOSE 8080 9090/tcp\n"
    'ENTRYPOINT ["python", "-m", "app"]\n'
    'CMD ["--port", "8080"]\n'
)


def test_stage_count_and_names():
    p = parse_source(_MULTISTAGE)
    assert [s.name for s in p.stages] == ["base", "final"]
    assert [s.index for s in p.stages] == [0, 1]


def test_global_arg_parsed():
    p = parse_source(_MULTISTAGE)
    assert p.args["PYTHON_VERSION"] == "3.12-slim"


def test_base_references():
    p = parse_source(_MULTISTAGE)
    assert p.stages[0].base == "python:${PYTHON_VERSION}"   # raw ref kept; substitution happens in build
    assert p.stages[1].base == "base"                        # internal multi-stage reference


def test_copy_sources_and_from():
    p = parse_source(_MULTISTAGE)
    base, final = p.stages
    assert base.copies[0].sources == ("requirements.txt",) and base.copies[0].from_stage is None
    # final has one host-context copy and one --from=base stage copy
    host = [c for c in final.copies if c.from_stage is None]
    staged = [c for c in final.copies if c.from_stage == "base"]
    assert host[0].sources == ("./app",)
    assert staged and staged[0].sources == ("/usr/local/lib",)


def test_expose_entrypoint_cmd():
    final = parse_source(_MULTISTAGE).stages[1]
    assert final.exposed_ports == ("8080", "9090/tcp")
    assert final.entrypoint == '["python", "-m", "app"]'
    assert final.cmd == '["--port", "8080"]'


def test_line_continuation():
    p = parse_source(
        "FROM alpine\n"
        "RUN apk add --no-cache \\\n"
        "    curl \\\n"
        "    bash\n"
        "COPY a \\\n"
        "     b \\\n"
        "     /dest/\n"
    )
    # the continued COPY collapses to one instruction: sources (a, b) -> dest /dest/
    copies = p.stages[0].copies
    assert len(copies) == 1 and copies[0].sources == ("a", "b") and copies[0].dest == "/dest/"


def test_comments_and_blanks_ignored():
    p = parse_source("# a comment\n\nFROM scratch\n\n# another\nWORKDIR /\n")
    assert len(p.stages) == 1 and p.stages[0].base == "scratch"


def test_unnamed_stage_has_no_name():
    p = parse_source("FROM ubuntu:22.04\nRUN echo hi\n")
    assert p.stages[0].name is None and p.stages[0].index == 0


def test_json_array_copy_form():
    p = parse_source('FROM alpine\nCOPY ["src/app.py", "/app/app.py"]\n')
    c = p.stages[0].copies[0]
    assert c.sources == ("src/app.py",) and c.dest == "/app/app.py"


def test_empty_file_is_empty():
    assert parse_source("") .stages == ()


def test_tab_between_keyword_and_args():
    # Docker allows a tab after the instruction keyword -- it must NOT drop the instruction.
    p = parse_source("FROM\talpine:3.20\nCOPY\tapp.py /app/\n")
    assert len(p.stages) == 1 and p.stages[0].base == "alpine:3.20"
    assert p.stages[0].copies[0].sources == ("app.py",)


def test_comment_line_inside_continuation_is_dropped():
    # A whole-line comment between continued lines must be discarded, not folded into the instruction.
    p = parse_source("FROM alpine\nCOPY a.py \\\n# a comment mid-continuation\n     b.py /app/\n")
    c = p.stages[0].copies[0]
    assert c.sources == ("a.py", "b.py") and c.dest == "/app/"


def test_json_copy_with_flags():
    # Docker permits flags before the JSON exec form: `COPY --chown=x ["a", "b"]`.
    p = parse_source('FROM alpine\nCOPY --chown=1000:1000 ["app.py", "/app/app.py"]\n')
    c = p.stages[0].copies[0]
    assert c.sources == ("app.py",) and c.dest == "/app/app.py"


def test_copy_from_with_json_form():
    # `--from` is honored even with the JSON exec form (flags are stripped before JSON detection).
    p = parse_source('FROM a AS build\nFROM b\nCOPY --from=build ["/out", "/app"]\n')
    c = p.stages[1].copies[0]
    assert c.from_stage == "build" and c.sources == ("/out",)


def test_json_copy_preserves_internal_whitespace():
    # A path with double spaces inside the JSON string must survive (not be collapsed by tokenizing).
    p = parse_source('FROM alpine\nCOPY ["a  b.py", "/app/a  b.py"]\n')
    c = p.stages[0].copies[0]
    assert c.sources == ("a  b.py",) and c.dest == "/app/a  b.py"


def test_bracket_glob_falls_back_to_shell_form():
    # `[ab].py` is a shell char-class glob, not JSON -> must parse as a shell COPY, not be dropped.
    p = parse_source("FROM alpine\nCOPY --chown=1000 [ab].py /app/\n")
    c = p.stages[0].copies[0]
    assert c.sources == ("[ab].py",) and c.dest == "/app/"


def test_resolve_base_leaves_modifier_form_intact():
    from auryn.ingestors.docker.build import _resolve_base

    # `${BASE:-$FALLBACK}` must stay verbatim -- the inner `$FALLBACK` must NOT be substituted.
    assert _resolve_base("${BASE:-$FALLBACK}", {"FALLBACK": "alpine"}) == "${BASE:-$FALLBACK}"
    # a plain `${NAME}` with a value still expands; a no-value ARG stays raw.
    assert _resolve_base("${BASE}", {"BASE": "python:3.12"}) == "python:3.12"
    assert _resolve_base("${BASE}", {"BASE": ""}) == "${BASE}"
