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
    Config with ``agents`` narrowed to those this run registered (empty if none detected).

    Resilient per agent: a detected agent whose registration fails (unreachable CLI, a config
    file we can't safely edit, ...) is logged and skipped so the others still get wired up."""
    adapters = default_adapters() if adapters is None else adapters
    command = server_command or default_server_command()
    load_config()  # an unreadable config.toml fails here, before any agent is touched
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
    # registered agents, it must not silently reset persisted settings. The manifest also keeps
    # an agent an earlier run registered and this one didn't (not detected from this shell, or
    # its registration failed) -- `uninstall` reverses only what the manifest lists.
    prior = load_config()  # as it is NOW: registering took a while, and an ingest may have saved
    config = replace(prior, backend=backend, agents=tuple(registrations))
    names = {r.name for r in registrations}
    save_config(replace(config, agents=tuple(a for a in prior.agents if a.name not in names) + config.agents))
    return config


def do_uninstall(*, purge: bool = False, force: bool = False, adapters=None) -> Config:
    """Reverse an install: stop the local server, deregister every recorded agent, then either
    clear the manifest (keep data) or purge ``~/.auryn`` entirely. An agent that can't be
    deregistered stays in the manifest -- and the home dir with it -- to retry from; `force`
    forgets it all the same (the way out when its CLI is gone for good). Returns the prior Config
    with ``agents`` narrowed to those deregistered (so the caller can report what was removed)."""
    from auryn.core.server_manager import stop_server

    prior = load_config()
    stop_server()

    by_name = {a.name: a for a in (default_adapters() if adapters is None else adapters)}
    failed = []
    for reg in prior.agents:
        adapter = by_name.get(reg.name)
        if adapter is not None:
            try:
                adapter.deregister(reg)
            except Exception as e:  # noqa: BLE001 -- e.g. its CLI is gone: the rest still get reversed
                logger.warning("Could not deregister %s (%s)", reg.name, e)
                failed.append(reg)

    if purge and (force or not failed):
        shutil.rmtree(home_dir(), ignore_errors=True)
    else:  # the config as it is NOW, like do_install
        save_config(replace(load_config(), agents=() if force else tuple(failed)))
    return replace(prior, agents=tuple(a for a in prior.agents if a not in failed))
