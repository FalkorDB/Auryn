"""Adapters that register/deregister the Auryn MCP server with a coding agent, behind a
tiny interface -- ``detect`` / ``register`` / ``deregister`` -- so adding an agent is a new
adapter, not new orchestration (see core/install.py).

Three shapes:
- **Claude Code / OpenAI Codex** register via their own ``<cli> mcp`` command (Claude also
  installs the skill). A CLI lets the agent own its config file format -- notably Codex keeps MCP
  servers in TOML (``~/.codex/config.toml``), which ``codex mcp add`` writes and preserves.
- **Antigravity, GitHub Copilot, Cursor, Windsurf, Gemini CLI, VS Code Copilot Chat** register by
  merging an entry into a JSON config file (:class:`McpConfigAgent`). Most use the standard
  ``mcpServers`` map, but VS Code uses a ``servers`` map with a ``type: stdio`` entry -- both are
  the same adapter parameterised by ``servers_key`` / ``entry_extra``.
- **Generic AGENTS.md / Aider** register by merging a delimited markdown section into an
  instructions file (:class:`AgentsMdAgent`) -- for agents that read repo/user guidance from
  Markdown rather than an MCP config. Aider has no stable native MCP config, so it is served this
  way (its conventions file, loaded with ``aider --read``).

Everything is *user-scoped* and outside the user's repo. All the moving parts (the subprocess
runner, the skills dir, the config file paths) are injectable, so every adapter is unit-testable
without the real agent installed or its real config touched.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from auryn.core.app_config import AgentRegistration

Runner = Callable[[list[str]], subprocess.CompletedProcess]

_SKILL_NAME = "auryn"
_SERVER_NAME = "auryn"
_MCP_SERVERS_KEY = "mcpServers"


def _default_run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def _skill_source() -> Path:
    """The bundled skill shipped inside the package (works for editable and wheel installs)."""
    return Path(__file__).resolve().parent.parent / "assets" / "skills" / _SKILL_NAME


# ── Claude Code (CLI-based) ───────────────────────────────────────────────────
class ClaudeCode:
    """Claude Code integration via its `claude mcp` CLI."""

    name = "claude-code"

    def __init__(
        self, run: Runner = _default_run, skills_dir: Path | None = None, config_path: Path | None = None
    ) -> None:
        self._run = run
        self._skills_dir = skills_dir
        self._config_path = config_path

    def detect(self) -> bool:
        """Present iff the `claude` CLI is on PATH."""
        return shutil.which("claude") is not None

    def _skills_root(self) -> Path:
        if self._skills_dir is not None:
            return self._skills_dir
        env = os.environ.get("AURYN_CLAUDE_SKILLS")  # test/override hook
        return Path(env).expanduser() if env else Path.home() / ".claude" / "skills"

    def _claude_config(self) -> Path:
        if self._config_path is not None:
            return self._config_path
        env = os.environ.get("AURYN_CLAUDE_CONFIG")  # test/override hook
        return Path(env).expanduser() if env else Path.home() / ".claude.json"

    def _enable_always_load(self) -> None:
        """Mark our server so Claude Code loads its tools into the agent's INITIAL tool list,
        instead of deferring them behind tool-search. `claude mcp add` has no flag for this, so we
        patch the config it just wrote (user scope -> ~/.claude.json, a `mcpServers` map). Without
        it, in a tool-heavy setup the code-graph tools are invisible until the agent happens to
        search for them -- so it often just greps instead (measured). Best-effort: if the config is
        missing or not valid JSON, leave it (the server still works, only deferred)."""
        path = self._claude_config()
        try:
            data = json.loads(path.read_text()) if path.exists() else None
        except ValueError:
            return  # don't clobber a config we can't round-trip
        if not isinstance(data, dict):
            return
        servers = data.get(_MCP_SERVERS_KEY)
        entry = servers.get(_SERVER_NAME) if isinstance(servers, dict) else None
        if isinstance(entry, dict) and entry.get("alwaysLoad") is not True:
            entry["alwaysLoad"] = True
            _write_config(path, data)

    def register(self, server_command: list[str]) -> AgentRegistration:
        """Register the MCP server (user scope) and install the skill. Idempotent: any prior
        registration under the same name is removed first, so re-running install is safe."""
        self._run(["claude", "mcp", "remove", "-s", "user", _SERVER_NAME])  # best-effort clear
        added = self._run(["claude", "mcp", "add", "-s", "user", _SERVER_NAME, "--", *server_command])
        if added.returncode != 0:
            raise RuntimeError(f"`claude mcp add` failed: {(added.stderr or added.stdout).strip()}")
        self._enable_always_load()  # surface the tools upfront, don't let them hide behind tool-search

        dest = self._skills_root() / _SKILL_NAME
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(_skill_source(), dest, dirs_exist_ok=True)
        return AgentRegistration(name=self.name, scope="user", server_name=_SERVER_NAME, skill_path=str(dest))

    def deregister(self, reg: AgentRegistration) -> None:
        """Reverse a register(): remove the MCP server and delete the installed skill. Best-effort
        and idempotent -- a no-op if the user already removed either."""
        self._run(["claude", "mcp", "remove", "-s", reg.scope, reg.server_name])
        if reg.skill_path:
            shutil.rmtree(reg.skill_path, ignore_errors=True)


# ── OpenAI Codex (CLI-based) ──────────────────────────────────────────────────
class CodexCli:
    """OpenAI Codex integration via its `codex mcp` CLI. Codex keeps MCP servers in TOML
    (~/.codex/config.toml); delegating to `codex mcp add` lets Codex own that format rather than
    us hand-editing TOML and risking clobbering the user's other config. No skill dir: Codex has
    no skills mechanism, so this registers the MCP server only (agents read it over MCP)."""

    name = "codex"

    def __init__(self, run: Runner = _default_run) -> None:
        self._run = run

    def detect(self) -> bool:
        """Present iff the `codex` CLI is on PATH."""
        return shutil.which("codex") is not None

    def register(self, server_command: list[str]) -> AgentRegistration:
        """Register the MCP server. Idempotent: clear any prior entry of the same name first, so
        re-running install is safe (mirrors the Claude Code adapter)."""
        self._run(["codex", "mcp", "remove", _SERVER_NAME])  # best-effort clear
        added = self._run(["codex", "mcp", "add", _SERVER_NAME, "--", *server_command])
        if added.returncode != 0:
            raise RuntimeError(f"`codex mcp add` failed: {(added.stderr or added.stdout).strip()}")
        return AgentRegistration(name=self.name, scope="user", server_name=_SERVER_NAME)

    def deregister(self, reg: AgentRegistration) -> None:
        """Reverse a register(): remove the MCP server. Best-effort and idempotent."""
        self._run(["codex", "mcp", "remove", reg.server_name])


# ── File-config agents (Antigravity / Copilot / Cursor / Windsurf / Gemini CLI) ─
def _read_config(path: Path) -> dict:
    """Parse an agent's MCP config, or {} if it doesn't exist yet. Raises if the file exists but
    isn't valid JSON -- so we refuse to clobber a config we can't safely round-trip (e.g. a JSONC
    file with comments) rather than silently wiping the user's other servers."""
    if not path.exists():
        return {}
    text = path.read_text()
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError as e:
        raise RuntimeError(f"{path} is not valid JSON (comments not supported); refusing to overwrite it") from e
    return data if isinstance(data, dict) else {}


def _write_config(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


@dataclass
class McpConfigAgent:
    """An agent that registers MCP servers by a JSON config file holding a map of named servers --
    Antigravity, GitHub Copilot (CLI), Cursor, Windsurf, Gemini CLI, VS Code Copilot Chat.
    Detection is "the agent's home dir exists"; register/deregister merge or remove *only* our
    entry, preserving everything else in the file. ``app_dir`` and ``config_path`` are plain fields
    so tests point them at a temp dir.

    The map key and entry shape are parameterised: most agents use ``mcpServers`` with a
    ``{command, args}`` entry, but VS Code uses ``servers`` and wants a ``type: "stdio"`` on the
    entry (see :func:`vscode_adapter`). ``servers_key`` / ``entry_extra`` cover that difference
    without a second class or the wrong schema."""

    name: str
    app_dir: Path  # detection: does the agent's config home exist?
    config_path: Path  # the JSON file with the servers map
    server_name: str = _SERVER_NAME
    servers_key: str = _MCP_SERVERS_KEY  # "mcpServers" for most; "servers" for VS Code
    entry_extra: dict | None = None  # merged into each entry, e.g. {"type": "stdio"} for VS Code

    def detect(self) -> bool:
        return self.app_dir.exists()

    def register(self, server_command: list[str]) -> AgentRegistration:
        data = _read_config(self.config_path)
        servers = data.get(self.servers_key)
        if not isinstance(servers, dict):
            servers = data[self.servers_key] = {}
        entry = {"command": server_command[0], "args": list(server_command[1:])}
        if self.entry_extra:
            entry = {**self.entry_extra, **entry}
        servers[self.server_name] = entry
        _write_config(self.config_path, data)
        return AgentRegistration(
            name=self.name, scope="user", server_name=self.server_name, config_path=str(self.config_path)
        )

    def deregister(self, reg: AgentRegistration) -> None:
        path = Path(reg.config_path) if reg.config_path else self.config_path
        try:
            data = _read_config(path)
        except RuntimeError:
            return  # can't safely edit an unparseable file; leave it alone
        servers = data.get(self.servers_key)
        if isinstance(servers, dict) and reg.server_name in servers:
            del servers[reg.server_name]
            _write_config(path, data)


def _home() -> Path:
    # Indirection so tests can monkeypatch a fake home if they exercise the real factories.
    return Path.home()


def antigravity_adapter() -> McpConfigAgent:
    # Google Antigravity (the IDE, 2.0, and CLI) share one global MCP config at
    # ~/.gemini/config/mcp_config.json, keyed by the standard `mcpServers` map with local servers
    # as {command, args} -- verified against Antigravity's docs (Nov 2025). NOTE: this is NOT the
    # Gemini CLI, which reads ~/.gemini/settings.json; both merely live under the shared ~/.gemini
    # namespace, so detection on ~/.gemini is deliberately broad (a gcloud/Gemini-CLI user without
    # Antigravity may match -- harmless, we just write a file Antigravity would read if installed).
    # For future remote-server support, Antigravity uses `serverUrl` (not `url`) unlike Cursor/VS Code.
    h = _home()
    return McpConfigAgent("antigravity", h / ".gemini", h / ".gemini" / "config" / "mcp_config.json")


def github_copilot_adapter() -> McpConfigAgent:
    h = _home()
    return McpConfigAgent("github-copilot", h / ".copilot", h / ".copilot" / "mcp-config.json")


def cursor_adapter() -> McpConfigAgent:
    h = _home()
    return McpConfigAgent("cursor", h / ".cursor", h / ".cursor" / "mcp.json")


def windsurf_adapter() -> McpConfigAgent:
    h = _home()
    return McpConfigAgent("windsurf", h / ".codeium", h / ".codeium" / "windsurf" / "mcp_config.json")


def gemini_cli_adapter() -> McpConfigAgent:
    # Gemini CLI reads ~/.gemini/settings.json with the standard `mcpServers` map ({command, args}
    # for local servers) -- the same shape as Cursor/Copilot, so it's a plain McpConfigAgent. This
    # is distinct from antigravity_adapter (config/mcp_config.json under the same ~/.gemini home):
    # both may register when ~/.gemini exists, each writing its own file, which is harmless. Merging
    # only the mcpServers entry preserves the user's other settings.json keys (theme, model, ...).
    h = _home()
    return McpConfigAgent("gemini-cli", h / ".gemini", h / ".gemini" / "settings.json")


def vscode_adapter() -> McpConfigAgent:
    # VS Code Copilot Chat reads a USER-scoped mcp.json in the user profile folder (Command Palette
    # -> "MCP: Open User Configuration"). VS Code's schema differs from Cursor/Copilot: the map key
    # is `servers` (not `mcpServers`) and a local server carries `type: "stdio"` alongside
    # {command, args} -- verified against VS Code's MCP configuration reference + "Add and manage MCP
    # servers in VS Code" docs (2026). We target the macOS profile path (matching this module's
    # other macOS-rooted adapters); `_home` is monkeypatchable and the fields are injectable so
    # tests point at a temp dir. Detection = the VS Code User dir exists (i.e. VS Code is installed).
    user = _home() / "Library" / "Application Support" / "Code" / "User"
    return McpConfigAgent("vscode", user, user / "mcp.json", servers_key="servers", entry_extra={"type": "stdio"})


# ── Markdown-instructions agents (generic AGENTS.md / Aider) ──────────────────
_AGENTS_MD_BEGIN = "<!-- BEGIN auryn (managed by `auryn install`) -->"
_AGENTS_MD_END = "<!-- END auryn -->"


def _agents_md_block(server_command: list[str]) -> str:
    """The delimited section we own inside an AGENTS.md/CONVENTIONS.md file. Idempotent by its BEGIN
    /END markers; text mirrors the bundled `auryn` skill so every surface gives the same advice:
    query the Auryn MCP server first for structural questions."""
    cmd = " ".join(server_command)
    body = f"""## Auryn (MCP server: `{_SERVER_NAME}`)

This repository can be indexed into a queryable **code graph** exposed by the `{_SERVER_NAME}` MCP
server (start command: `{cmd}`). For any *structural* question about the codebase -- where a symbol
is defined, who calls it / what it calls, the path between two symbols, or the blast radius of a
change -- **query the code graph first** instead of grepping and reading whole files. One query
returns the precise answer with node ids, at a fraction of the tokens.

Typical flow: `auryn_status` -> `index_repository(path=".")` if it reports no data ->
`locate(name=...)` to turn a symbol name into its id -> answer with the smallest tool that fits
(`who_calls`, `callees`, `find_path`, `impact_of`, `trace_entrypoint`, `get_context`). Cite the
node id the graph returns as your evidence.

Agents that read this file but do not speak MCP (e.g. Aider) still benefit: prefer the graph's
structural view over ad-hoc file spelunking when reasoning about the code."""
    return f"{_AGENTS_MD_BEGIN}\n{body}\n{_AGENTS_MD_END}"


def _merge_md_section(text: str, block: str) -> str:
    """Insert or replace our delimited block, preserving all surrounding text. Idempotent: a second
    call replaces the previous block in place rather than appending a duplicate."""
    if _AGENTS_MD_BEGIN in text and _AGENTS_MD_END in text:
        start = text.index(_AGENTS_MD_BEGIN)
        end = text.index(_AGENTS_MD_END) + len(_AGENTS_MD_END)
        return text[:start] + block + text[end:]
    if not text.strip():
        return block + "\n"
    return text.rstrip("\n") + "\n\n" + block + "\n"


def _strip_md_section(text: str) -> str:
    """Remove our delimited block (and its padding), leaving the rest of the file intact."""
    if _AGENTS_MD_BEGIN not in text or _AGENTS_MD_END not in text:
        return text
    start = text.index(_AGENTS_MD_BEGIN)
    end = text.index(_AGENTS_MD_END) + len(_AGENTS_MD_END)
    before, after = text[:start], text[end:]
    if before.strip() and after.strip():
        return before.rstrip("\n") + "\n\n" + after.lstrip("\n")
    return before.strip("\n") + after.strip("\n")


@dataclass
class AgentsMdAgent:
    """An agent configured by a Markdown instructions file (AGENTS.md / CONVENTIONS.md) rather than
    an MCP config -- we merge a clearly-delimited `auryn` section telling the agent to use the
    Auryn MCP tools first. The merge is idempotent (BEGIN/END markers) and never clobbers the
    rest of the file. ``path`` and ``detector`` are plain fields so tests point them at a temp dir.

    Detection: ``detector`` if given, else always True. The *generic* AGENTS.md target is a
    universal, agent-agnostic fallback -- writing a delimited, idempotent Markdown block is always
    safe and any AGENTS.md-aware agent benefits -- so it detects True everywhere. The Aider target
    passes a ``detector`` gated on the ``aider`` CLI being installed, since it writes Aider's own
    conventions file."""

    name: str
    path: Path  # the Markdown file to write/merge
    detector: Callable[[], bool] | None = None
    server_name: str = _SERVER_NAME

    def detect(self) -> bool:
        return self.detector() if self.detector is not None else True

    def register(self, server_command: list[str]) -> AgentRegistration:
        text = self.path.read_text() if self.path.exists() else ""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(_merge_md_section(text, _agents_md_block(server_command)))
        return AgentRegistration(
            name=self.name, scope="user", server_name=self.server_name, config_path=str(self.path)
        )

    def deregister(self, reg: AgentRegistration) -> None:
        path = Path(reg.config_path) if reg.config_path else self.path
        if not path.exists():
            return
        stripped = _strip_md_section(path.read_text())
        if stripped.strip():
            path.write_text(stripped.rstrip("\n") + "\n")
        else:
            path.unlink()  # file held only our section (we created it) -> remove it


def agents_md_adapter() -> AgentsMdAgent:
    # Generic AGENTS.md target: the emerging cross-agent convention is a Markdown instructions file
    # many tools read. Install is user-scoped, so we write a user-global ~/AGENTS.md. Detection is
    # deliberately always-True: this is the agent-agnostic fallback, and merging an idempotent,
    # delimited section into ~/AGENTS.md is harmless even if nothing reads it.
    return AgentsMdAgent("agents-md", _home() / "AGENTS.md")


def aider_adapter() -> AgentsMdAgent:
    # Aider has no stable native MCP configuration (its docs describe none; MCP is only third-party
    # /experimental), but it *does* load Markdown convention files into context via `aider --read`
    # (or a `read:` entry in .aider.conf.yml). So the best available integration is to write our
    # Auryn guidance into Aider's conventions file (~/CONVENTIONS.md, Aider's documented
    # default name); the user loads it with `aider --read ~/CONVENTIONS.md`. Detection is gated on
    # the `aider` CLI being on PATH so we don't drop a file for users who don't have Aider.
    return AgentsMdAgent("aider", _home() / "CONVENTIONS.md", detector=lambda: shutil.which("aider") is not None)


def default_adapters() -> list:
    """Every agent install/uninstall considers; only *detected* ones are actually registered."""
    return [
        ClaudeCode(),
        CodexCli(),
        antigravity_adapter(),
        github_copilot_adapter(),
        cursor_adapter(),
        windsurf_adapter(),
        gemini_cli_adapter(),
        vscode_adapter(),
        agents_md_adapter(),
        aider_adapter(),
    ]
