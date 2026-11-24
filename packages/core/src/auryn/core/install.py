"""Orchestration for ``auryn install`` / ``uninstall``.

install: pick a backend, register the MCP server + skill with every detected agent, and record
what was done (backend + an ``[install]`` manifest) in ``~/.auryn/config.toml``.
uninstall: stop the local server, reverse the manifest agent-by-agent, take our resync block back
out of every registered repo's git hooks, then keep or purge the home dir. Nothing else here (or
in an adapter) ever writes into a user's repo.

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
    # Preserve everything else the user already configured (resync trigger, disabled hooks,
    # registered repos, embed mode) -- a re-install only updates the backend and the set of
    # registered agents, it must not silently reset persisted settings.
    config = replace(load_config(), backend=backend, agents=tuple(registrations))
    save_config(config)
    return config


def do_uninstall(*, purge: bool = False, adapters=None) -> Config:
    """Reverse an install: stop the local server, deregister every recorded agent, then either
    clear the manifest (keep data) or purge ``~/.auryn`` entirely. Returns the prior Config
    (so the caller can report what was removed)."""
    from auryn.core import hooks
    from auryn.core.server_manager import stop_server

    prior = load_config()
    stop_server()

    by_name = {a.name: a for a in (default_adapters() if adapters is None else adapters)}
    for reg in prior.agents:
        adapter = by_name.get(reg.name)
        if adapter is not None:
            adapter.deregister(reg)
    # The `commit` trigger put a resync block in each registered repo's git hooks. Left behind, the
    # next commit there would run `auryn sync` again -- recreating the home dir we are removing.
    stranded = []  # repos still carrying a hook of ours: they stay registered, to retry from
    for repo in prior.repos:
        try:
            hooks.uninstall(repo.local_dir)
        except Exception as e:  # noqa: BLE001 -- one unwritable hook must not strand the rest
            logger.warning("Could not remove the resync hooks from %s (%s)", repo.local_dir, e)
            stranded.append(repo)

    if purge:
        shutil.rmtree(home_dir(), ignore_errors=True)
    else:
        save_config(replace(load_config(), agents=(), repos=tuple(stranded)))  # the rest stays as it is
    return prior
