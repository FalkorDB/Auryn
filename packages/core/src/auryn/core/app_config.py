"""The Auryn machine config (``~/.auryn/config.toml``): which FalkorDB backend to
use, plus an install manifest recording what ``install`` touched so ``uninstall`` can reverse
it precisely.

Two backends, one runtime contract -- both resolve to "connect to a FalkorDB at host:port":

- ``local``  -- a bundled falkordb-lite server Auryn starts on demand (no Docker). The
  host/port aren't stored here; they're discovered at runtime (a random port), see
  ``server_manager``.
- ``remote`` -- an existing FalkorDB the user points at (host/port/creds stored here).

Absent config defaults to ``local`` so the CLI and the lazy server work on a fresh machine
before ``install`` has ever run. Reading uses stdlib ``tomllib``; writing uses ``tomli_w``
(imported lazily in :func:`save_config`, so the read path -- e.g. the daemon -- never pulls it).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, replace

from auryn.core.home import config_path


@dataclass(frozen=True)
class Backend:
    """The resolved backend. For ``local`` only ``mode`` is meaningful (host/port are runtime
    discovered); for ``remote`` the connection fields point at the user's server."""

    mode: str = "local"  # "local" (bundled falkordb-lite) | "remote"
    host: str | None = None
    port: int | None = None
    username: str | None = None
    password: str | None = None
    ssl: bool = False


@dataclass(frozen=True)
class AgentRegistration:
    """One agent ``install`` registered the MCP server with -- enough for ``uninstall`` to
    reverse it exactly (which agent, which scope, and where its copy of the skill went)."""

    name: str  # e.g. "claude-code"
    scope: str = "user"
    server_name: str = "auryn"  # the name under which the MCP server was registered
    skill_path: str | None = None  # directory the skill was copied to, if any (Claude Code)
    config_path: str | None = None  # the agent config file our entry was merged into (file-based agents)


VALID_TRIGGERS = ("manual", "commit", "save")


@dataclass(frozen=True)
class RepoRegistration:
    """One ingested repo, keyed by its local working directory (ADR 0003). Only what can't be
    re-derived is stored: `local_dir` (authoritative -- a URI clone lands in the user's CWD),
    and `graph_name`/`project_name`/`trigger` ONLY when they deviate from the derived / install
    default (None otherwise). The remote, languages, shas, and counts come from git / the graph
    on demand. Its job is the trigger machinery (watch-list, hook target, forget)."""

    local_dir: str
    graph_name: str | None = None
    project_name: str | None = None
    trigger: str | None = None  # None -> the install-wide [resync].trigger
    disabled_hooks: tuple[str, ...] = ()  # post hooks turned off for this repo (additive to install-wide)


@dataclass(frozen=True)
class Config:
    backend: Backend = field(default_factory=Backend)
    agents: tuple[AgentRegistration, ...] = ()
    resync_trigger: str = "commit"  # install-wide default: manual | commit | save
    repos: tuple[RepoRegistration, ...] = ()
    disabled_hooks: tuple[str, ...] = ()  # post hooks turned off machine-wide (see core/registry.py)
    embed_mode: str = "background"  # inline | background | off -- how the always-on embed runs (see embedder.embed_mode)
    git_info_enabled: bool = False  # collect local git history and public GitHub metadata on ingest


def _parse_backend(section: dict) -> Backend:
    if section.get("mode") == "remote":
        return Backend(
            mode="remote",
            host=section.get("host", "localhost"),
            port=int(section.get("port", 6379)),
            username=section.get("username"),
            password=section.get("password"),
            ssl=bool(section.get("ssl", False)),
        )
    return Backend(mode="local")


def load_config() -> Config:
    """The full config (backend + install manifest). Missing file -> defaults (local, no
    registered agents)."""
    path = config_path()
    if not path.exists():
        return Config()
    data = tomllib.loads(path.read_text())
    backend = _parse_backend(data.get("backend", {}))
    agents = tuple(
        AgentRegistration(
            name=a["name"],
            scope=a.get("scope", "user"),
            server_name=a.get("server_name", "auryn"),
            skill_path=a.get("skill_path"),
            config_path=a.get("config_path"),
        )
        for a in data.get("install", {}).get("agents", [])
    )
    if not all(isinstance(a.name, str) for a in agents):  # it keys the manifest: fail here, not mid-install
        raise ValueError("an [[install.agents]] name is not text")
    trigger = data.get("resync", {}).get("trigger", "commit")
    if trigger not in VALID_TRIGGERS:
        trigger = "commit"
    repos = tuple(
        RepoRegistration(
            local_dir=r["local_dir"],
            graph_name=r.get("graph_name"),
            project_name=r.get("project_name"),
            trigger=r.get("trigger"),
            disabled_hooks=tuple(r.get("disabled_hooks", [])),
        )
        for r in data.get("repo", [])
        if r.get("local_dir") and isinstance(r["local_dir"], str)  # a folder: `local_dir = 123` names none
    )
    disabled_hooks = tuple(data.get("hooks", {}).get("disabled", []))
    embed_mode = str(data.get("embeddings", {}).get("mode", "background")).strip().lower()
    if embed_mode not in ("inline", "background", "off"):
        embed_mode = "background"  # a stray value must never break an ingest; fall back to the default
    # Robust against a malformed config: `git_info = false` (a bare value, not a table) would make
    # `.get("enabled")` raise AttributeError and crash every ingest; `enabled = "false"` (a string)
    # is truthy under bool() and would silently opt IN. Require a real TOML boolean `true` in a table.
    gi = data.get("git_info", {})
    git_info_enabled = isinstance(gi, dict) and gi.get("enabled") is True
    return Config(backend=backend, agents=agents, resync_trigger=trigger, repos=repos,
                  disabled_hooks=disabled_hooks, embed_mode=embed_mode,
                  git_info_enabled=git_info_enabled)


def load_backend() -> Backend:
    """Just the backend -- the hot path every command/daemon uses to get a connection. Missing
    or unknown ``mode`` -> ``local`` (the fresh-machine default)."""
    return load_config().backend


def save_config(config: Config) -> None:
    """Write ``~/.auryn/config.toml`` (creating the home dir), ``chmod 600`` since it may
    hold a remote password. tomli_w is imported here so the read-only path never needs it."""
    import tomli_w

    b = config.backend
    doc: dict = {"backend": {"mode": b.mode}}
    if b.mode == "remote":
        doc["backend"].update(
            {k: v for k, v in {
                "host": b.host,
                "port": b.port,
                "username": b.username,
                "password": b.password,
                "ssl": b.ssl,
            }.items() if v is not None}
        )
    if config.agents:
        doc["install"] = {
            "agents": [
                {k: v for k, v in {
                    "name": a.name,
                    "scope": a.scope,
                    "server_name": a.server_name,
                    "skill_path": a.skill_path,
                    "config_path": a.config_path,
                }.items() if v is not None}
                for a in config.agents
            ]
        }
    if config.resync_trigger != "commit":
        doc["resync"] = {"trigger": config.resync_trigger}
    if config.disabled_hooks:
        doc["hooks"] = {"disabled": list(config.disabled_hooks)}
    if config.embed_mode and config.embed_mode != "background":
        doc["embeddings"] = {"mode": config.embed_mode}  # only persist a non-default mode
    if config.git_info_enabled:
        doc["git_info"] = {"enabled": True}
    if config.repos:
        doc["repo"] = [
            {k: v for k, v in {
                "local_dir": r.local_dir,
                "graph_name": r.graph_name,
                "project_name": r.project_name,
                "trigger": r.trigger,
                "disabled_hooks": list(r.disabled_hooks) if r.disabled_hooks else None,
            }.items() if v is not None}
            for r in config.repos
        ]

    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomli_w.dumps(doc))
    path.chmod(0o600)


# -- ingest registry helpers (keyed by local_dir) --------------------------------------------


def set_resync_trigger(trigger: str) -> None:
    """Persist the install-wide default resync trigger."""
    from dataclasses import replace

    if trigger not in VALID_TRIGGERS:
        raise ValueError(f"trigger must be one of {VALID_TRIGGERS}, got {trigger!r}")
    save_config(replace(load_config(), resync_trigger=trigger))


def set_embed_mode(mode: str) -> None:
    """Persist how auto-embed runs: ``inline`` (synchronous, blocks the ingest), ``background``
    (hand to the detached embed daemon; the default), or ``off``. Durable in ~/.auryn/config.toml;
    AURYN_EMBED_MODE is the per-run override (see embeddings.embedder.embed_mode)."""
    from dataclasses import replace

    if mode not in ("inline", "background", "off"):
        raise ValueError(f"embed mode must be one of ('inline', 'background', 'off'), got {mode!r}")
    save_config(replace(load_config(), embed_mode=mode))


def set_git_info_enabled(enabled: bool) -> None:
    """Enable or disable local Git and public GitHub metadata collection."""
    save_config(replace(load_config(), git_info_enabled=bool(enabled)))


def register_repo(reg: RepoRegistration) -> None:
    """Upsert a repo registration (replacing any existing entry for the same local_dir)."""
    from dataclasses import replace

    cfg = load_config()
    kept = tuple(r for r in cfg.repos if r.local_dir != reg.local_dir)
    save_config(replace(cfg, repos=kept + (reg,)))


def unregister_repo(local_dir: str) -> RepoRegistration | None:
    """Remove and return a repo registration by local_dir (None if it wasn't registered)."""
    from dataclasses import replace

    cfg = load_config()
    match = next((r for r in cfg.repos if r.local_dir == local_dir), None)
    if match is not None:
        save_config(replace(cfg, repos=tuple(r for r in cfg.repos if r.local_dir != local_dir)))
    return match


def get_repo(local_dir: str) -> RepoRegistration | None:
    return next((r for r in load_config().repos if r.local_dir == local_dir), None)


def registered_project_name(reg: RepoRegistration) -> str:
    """The project a registered repo is ingested as: the stored `project_name`, else the folder's
    own name -- what `ingest_directory` uses when no name was given, the case where none is stored."""
    from pathlib import Path

    return reg.project_name or Path(reg.local_dir).name or "repo"


def effective_trigger(reg: RepoRegistration | None) -> str:
    """A repo's trigger: its per-repo override, else the install-wide default."""
    if reg is not None and reg.trigger is not None:
        return reg.trigger
    return load_config().resync_trigger


# -- hook enable/disable (post hooks; see core/registry.py) -----------------------------------


def effective_disabled_hooks(local_dir: str | None) -> set[str]:
    """Post hooks disabled for a repo: the install-wide set, plus this repo's per-repo set
    (additive). `local_dir=None` (e.g. a URI ingest with no registered dir) -> install-wide only."""
    cfg = load_config()
    disabled = set(cfg.disabled_hooks)
    if local_dir is not None:
        reg = next((r for r in cfg.repos if r.local_dir == local_dir), None)
        if reg is not None:
            disabled |= set(reg.disabled_hooks)
    return disabled


def set_hook_disabled(name: str, disabled: bool, local_dir: str | None = None) -> None:
    """Persist a hook's enabled/disabled state -- install-wide (`local_dir=None`) or for one
    repo. Disabling adds it to the relevant list; enabling removes it. Idempotent; a no-op
    write is skipped. A per-repo call upserts the repo's registration (creating a minimal one
    if it wasn't registered), mirroring the per-repo `trigger` override."""
    cfg = load_config()
    if local_dir is None:
        current = set(cfg.disabled_hooks)
        new = (current | {name}) if disabled else (current - {name})
        if new != current:
            save_config(replace(cfg, disabled_hooks=tuple(sorted(new))))
        return

    reg = next((r for r in cfg.repos if r.local_dir == local_dir), None)
    current = set(reg.disabled_hooks) if reg is not None else set()
    new = (current | {name}) if disabled else (current - {name})
    if reg is not None and new == current:
        return
    updated = replace(reg if reg is not None else RepoRegistration(local_dir=local_dir),
                      disabled_hooks=tuple(sorted(new)))
    kept = tuple(r for r in cfg.repos if r.local_dir != local_dir)
    save_config(replace(cfg, repos=kept + (updated,)))
