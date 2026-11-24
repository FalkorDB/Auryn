"""Every `auryn` command (and every option) must stay documented -- so a new command or flag
can't ship without help text. Walks the whole click command tree, including the dynamically
registered per-ingestor `run`/`git-sync` subcommands."""

from __future__ import annotations

import click

from auryn.cli import _attach_ingestor_commands, cli

_MIN_HELP = 15  # a real sentence, not a stray word


def _all_commands():
    """(dotted-name, command) for every command and subcommand in the tree."""
    _attach_ingestor_commands()  # pull in run/git-sync <ingestor> subcommands
    out: list[tuple[str, click.Command]] = []

    def walk(cmd, path):
        out.append((" ".join(path), cmd))
        if isinstance(cmd, click.Group):
            for name in sorted(cmd.commands):
                walk(cmd.commands[name], path + [name])

    for name in sorted(cli.commands):
        walk(cli.commands[name], [name])
    return out


def test_every_command_has_help():
    thin = [
        name for name, cmd in _all_commands()
        if len((cmd.help or cmd.short_help or "").strip()) < _MIN_HELP
    ]
    assert not thin, f"commands missing/thin help: {thin}"


def test_every_option_has_help():
    missing = [
        f"{name} {'/'.join(p.opts)}"
        for name, cmd in _all_commands()
        for p in cmd.params
        if isinstance(p, click.Option) and p.name != "help" and not (p.help or "").strip()
    ]
    assert not missing, f"options missing help: {missing}"


def test_reference_doc_lists_every_top_level_command(tmp_path):
    """docs/cli.md is the consolidated reference -- it must mention every user-facing top-level
    command so it can't silently fall out of date when a command is added. Hidden commands
    (internal plumbing like the embed daemon) aren't part of the user reference."""
    from pathlib import Path

    doc = (Path(__file__).resolve().parents[2] / "docs" / "cli.md").read_text()
    missing = [name for name, cmd in cli.commands.items() if not cmd.hidden and name not in doc]
    assert not missing, f"docs/cli.md is missing commands: {missing}"
