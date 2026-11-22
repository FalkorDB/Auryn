"""The CLI's stdio fix: on a Windows console or redirect the locale codec (cp1252) cannot
encode the Unicode the CLI prints, so `auryn install` crashed mid-summary. Pure stdio
tests -- no database, no real agents."""

from __future__ import annotations

import io
import sys

import click

from click.testing import CliRunner

from auryn.cli import _ensure_utf8_stdio, cli


def _wrapped(encoding: str) -> tuple[io.TextIOWrapper, io.BytesIO]:
    buffer = io.BytesIO()
    return io.TextIOWrapper(buffer, encoding=encoding), buffer


def test_cp1252_stdout_is_reconfigured_to_utf8(monkeypatch):
    stdout, buffer = _wrapped("cp1252")
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", _wrapped("cp1252")[0])

    _ensure_utf8_stdio()

    print("\u2192", end="", file=sys.stdout)  # would raise UnicodeEncodeError before the fix
    sys.stdout.flush()
    assert "\u2192".encode("utf-8") in buffer.getvalue()


def test_utf8_stdout_is_left_alone(monkeypatch):
    stdout, _ = _wrapped("utf-8")
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", _wrapped("utf-8")[0])

    _ensure_utf8_stdio()

    assert sys.stdout.encoding.lower().replace("-", "") == "utf8"


def test_non_textiowrapper_streams_are_untouched(monkeypatch):
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", io.StringIO())

    _ensure_utf8_stdio()  # must not raise on a capture buffer

    assert isinstance(sys.stdout, io.StringIO)


def test_cli_group_invocation_calls_the_fix(monkeypatch):
    # CliRunner swaps in its own capture streams, so here we only prove the wiring:
    # the group callback must run the stdio fix before anything echoes.
    called = []
    monkeypatch.setattr("auryn.cli._ensure_utf8_stdio", lambda: called.append(True))

    @cli.command("stdio-probe")
    def _probe() -> None:
        click.echo("ok")

    result = CliRunner().invoke(cli, ["stdio-probe"])

    assert result.exit_code == 0
    assert called == [True]