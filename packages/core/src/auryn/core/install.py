"""Orchestration for ``auryn install`` / ``uninstall``.

install: pick a backend, register the MCP server + skill with every detected agent, and record
what was done (backend + an ``[install]`` manifest) in ``~/.auryn/config.toml``.
uninstall: stop the local server, reverse the manifest agent-by-agent, then keep or purge the
home dir. Nothing here (or in an adapter) ever writes into a user's repo.

The agents list and the MCP server command are injectable so the orchestration is testable
without a real ``claude`` CLI. See core/agents.py and docs/adr/0001.
"""

from __future__ import annotations

import logging
import shutil
import sys
from dataclasses import replace

from auryn.core.agents import default_adapters
from auryn.core.app_config import Backend, Config, load_config, save_config
from auryn.core.home import home_dir

logger = logging.getLogger("auryn.install")


def default_server_command() -> list[str]:
    """How an agent should launch our MCP server. ``python -m auryn mcp`` with the current
    interpreter -- stable across install methods (uv tool / pipx / venv) and independent of PATH."""
    return [sys.executable, "-m", "auryn", "mcp"]


def do_install(backend: Backend, *, adapters=None, server_command: list[str] | None = None) -> Config:
    """Register with every detected agent and persist the config + manifest. Returns the saved
    Config (its ``agents`` are exactly those that were registered -- empty if none detected).

    Resilient per agent: a detected agent whose registration fails (unreachable CLI, a config
    file we can't safely edit, ...) is logged and skipped so the others still get wired up."""
    adapters = default_adapters() if adapters is None else adapters
    command = server_command or default_server_command()
    registrations = []
    for adapter in adapters:
        if not adapter.detect():
            continue
        try:
            registrations.append(adapter.register(command))
        except Exception as e:  # noqa: BLE001 -- one bad agent must not fail the whole install
            logger.warning("Skipped %s: could not register (%s)", adapter.name, e)
    # Only the backend + manifest are install's to rewrite: a re-run must keep the repo registry,
    # the resync trigger, disabled hooks and the embed setting already in config.toml.
    config = replace(load_config(), backend=backend, agents=tuple(registrations))
    save_config(config)
    return config


def do_uninstall(*, purge: bool = False, adapters=None) -> Config:
    """Reverse an install: stop the local server, deregister every recorded agent, then either
    clear the manifest (keep data) or purge ``~/.auryn`` entirely. Returns the prior Config
    (so the caller can report what was removed)."""
    from auryn.core.server_manager import stop_server

    prior = load_config()
    stop_server()

    by_name = {a.name: a for a in (default_adapters() if adapters is None else adapters)}
    for reg in prior.agents:
        adapter = by_name.get(reg.name)
        if adapter is not None:
            adapter.deregister(reg)

    if purge:
        shutil.rmtree(home_dir(), ignore_errors=True)
    else:
        save_config(replace(prior, agents=()))
    return prior
