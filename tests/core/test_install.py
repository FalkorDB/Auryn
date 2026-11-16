"""install/uninstall orchestration (with a fake agent adapter) and the Claude Code adapter's
register/deregister (with a fake command runner + temp skills dir). No real `claude` CLI, no
touching a real agent config."""

from __future__ import annotations

import json
import subprocess

import pytest

from auryn.core.app_config import (
    AgentRegistration,
    Backend,
    Config,
    RepoRegistration,
    load_config,
    save_config,
)
from auryn.core.install import do_install, do_uninstall


class _FakeAgent:
    """Records register/deregister calls; always 'detected'."""

    name = "fake"

    def __init__(self):
        self.registered_command = None
        self.deregistered = None

    def detect(self):
        return True

    def register(self, server_command):
        self.registered_command = server_command
        return AgentRegistration(name=self.name, scope="user", server_name="auryn", skill_path="/tmp/skill")

    def deregister(self, reg):
        self.deregistered = reg


def test_install_registers_detected_agents_and_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "cg"))
    agent = _FakeAgent()
    cfg = do_install(Backend(mode="local"), adapters=[agent], server_command=["PY", "-m", "auryn", "mcp"])

    assert agent.registered_command == ["PY", "-m", "auryn", "mcp"]
    assert [a.name for a in cfg.agents] == ["fake"]
    assert [a.name for a in load_config().agents] == ["fake"]  # written to config.toml


def test_install_skips_undetected_agents(tmp_path, monkeypatch):
    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "cg"))

    class _Absent:
        name = "absent"

        def detect(self):
            return False

        def register(self, server_command):  # pragma: no cover - must not be called
            raise AssertionError("register called for an undetected agent")

    cfg = do_install(Backend(mode="local"), adapters=[_Absent()], server_command=["x"])
    assert cfg.agents == ()


def test_uninstall_reverses_manifest_and_clears(tmp_path, monkeypatch):
    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "cg"))
    agent = _FakeAgent()
    do_install(Backend(mode="local"), adapters=[agent], server_command=["x"])

    prior = do_uninstall(adapters=[agent])
    assert agent.deregistered is not None and agent.deregistered.name == "fake"
    assert [a.name for a in prior.agents] == ["fake"]  # returns what it removed
    assert load_config().agents == ()  # manifest cleared, backend kept


def test_reinstall_and_uninstall_keep_the_rest_of_the_config(tmp_path, monkeypatch):
    """`install` is re-runnable and a plain `uninstall` keeps the data: both rewrite the backend /
    agents only. The repo registry, a disabled hook and the embed setting must survive -- losing
    the registry orphans every ingested repo's graph name and hook target."""
    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "cg"))
    repo = RepoRegistration(local_dir="/work/api", graph_name="shared", project_name="api", trigger="save")
    save_config(Config(repos=(repo,), disabled_hooks=("http-endpoints",), auto_embed=True))

    agent = _FakeAgent()
    do_install(Backend(mode="remote", host="db", port=6400), adapters=[agent], server_command=["x"])
    cfg = load_config()
    assert (cfg.backend.mode, cfg.backend.host, [a.name for a in cfg.agents]) == ("remote", "db", ["fake"])
    assert (cfg.repos, cfg.disabled_hooks, cfg.auto_embed) == ((repo,), ("http-endpoints",), True)

    do_uninstall(adapters=[agent])
    cfg = load_config()
    assert (cfg.agents, cfg.backend.host) == ((), "db")
    assert (cfg.repos, cfg.disabled_hooks, cfg.auto_embed) == ((repo,), ("http-endpoints",), True)


def test_reinstall_keeps_an_agent_this_run_did_not_register(tmp_path, monkeypatch):
    """The manifest is what `uninstall` reverses, so a re-run must not drop an agent an earlier run
    registered just because it isn't detected now (its CLI off PATH in this shell): the entry
    would stay in that agent's config for good. The returned Config still lists this run only."""
    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "cg"))
    save_config(Config(agents=(AgentRegistration(name="cursor", config_path="/home/u/.cursor/mcp.json"),)))

    for _ in range(2):  # a second re-run replaces `fake`'s entry, never duplicates it
        cfg = do_install(Backend(mode="local"), adapters=[_FakeAgent()], server_command=["x"])
        assert [a.name for a in cfg.agents] == ["fake"]
        assert [a.name for a in load_config().agents] == ["cursor", "fake"]


def test_install_stops_on_an_unreadable_config_before_touching_any_agent(tmp_path, monkeypatch):
    """A config.toml that isn't valid TOML: `install` used to register every agent and only then
    crash reading it -- agents wired up, no manifest to reverse them. It now stops first (the CLI
    with one line naming the file) and leaves both the agents and the file alone."""
    import tomllib

    from click.testing import CliRunner

    from auryn.cli import cli

    home = tmp_path / "cg"
    home.mkdir()
    (home / "config.toml").write_text("[backend\n")
    monkeypatch.setenv("AURYN_HOME", str(home))
    agent = _FakeAgent()
    monkeypatch.setattr("auryn.core.install.default_adapters", lambda: [agent])

    with pytest.raises(tomllib.TOMLDecodeError):
        do_install(Backend(mode="local"), adapters=[agent], server_command=["x"])
    result = CliRunner().invoke(cli, ["install", "-y"])

    assert result.exit_code == 1 and "config.toml isn't valid TOML" in result.output
    assert agent.registered_command is None
    assert (home / "config.toml").read_text() == "[backend\n"


def test_uninstall_purge_removes_home(tmp_path, monkeypatch):
    home = tmp_path / "cg"
    monkeypatch.setenv("AURYN_HOME", str(home))
    do_install(Backend(mode="local"), adapters=[_FakeAgent()], server_command=["x"])
    assert home.exists()
    do_uninstall(purge=True, adapters=[_FakeAgent()])
    assert not home.exists()


# ── Claude Code adapter (fake runner; never invokes the real CLI) ────────────
def _ok(cmd):
    return subprocess.CompletedProcess(cmd, 0, "", "")


def test_claudecode_register_then_deregister(tmp_path):
    from auryn.core.agents import ClaudeCode

    ran: list[list[str]] = []

    def run(cmd):
        ran.append(cmd)
        return _ok(cmd)

    cc = ClaudeCode(run=run, skills_dir=tmp_path / "skills")
    reg = cc.register(["PY", "-m", "auryn", "mcp"])

    assert ["claude", "mcp", "add", "-s", "user", "auryn", "--", "PY", "-m", "auryn", "mcp"] in ran
    assert (tmp_path / "skills" / "auryn" / "SKILL.md").exists()  # bundled skill copied
    assert reg.skill_path == str(tmp_path / "skills" / "auryn")

    cc.deregister(reg)
    assert ["claude", "mcp", "remove", "-s", "user", "auryn"] in ran
    assert not (tmp_path / "skills" / "auryn").exists()  # skill removed


def test_claudecode_register_raises_when_add_fails(tmp_path):
    from auryn.core.agents import ClaudeCode

    def run(cmd):
        rc = 1 if "add" in cmd else 0  # `remove` (idempotent clear) ok, `add` fails
        return subprocess.CompletedProcess(cmd, rc, "", "boom")

    cc = ClaudeCode(run=run, skills_dir=tmp_path / "skills")
    with pytest.raises(RuntimeError, match="claude mcp add"):
        cc.register(["x"])


def test_codex_register_then_deregister():
    from auryn.core.agents import CodexCli

    ran: list[list[str]] = []

    def run(cmd):
        ran.append(cmd)
        return _ok(cmd)

    cx = CodexCli(run=run)
    reg = cx.register(["PY", "-m", "auryn", "mcp"])

    # Delegates to `codex mcp add` so Codex owns its TOML config; idempotent clear runs first.
    assert ["codex", "mcp", "add", "auryn", "--", "PY", "-m", "auryn", "mcp"] in ran
    assert reg.name == "codex" and reg.server_name == "auryn"

    cx.deregister(reg)
    assert ["codex", "mcp", "remove", "auryn"] in ran


def test_codex_register_raises_when_add_fails():
    from auryn.core.agents import CodexCli

    def run(cmd):
        rc = 1 if "add" in cmd else 0  # `remove` (idempotent clear) ok, `add` fails
        return subprocess.CompletedProcess(cmd, rc, "", "boom")

    with pytest.raises(RuntimeError, match="codex mcp add"):
        CodexCli(run=run).register(["x"])


# ── `install -y` auto-selection (the flag resolves a backend with no prompts) ─
def test_yes_auto_selects_local():
    from auryn.cli import _prompt_backend

    assert _prompt_backend(None, None, None, None, None, False, assume_yes=True).mode == "local"


def test_explicit_backend_local_needs_no_prompt():
    from auryn.cli import _prompt_backend

    assert _prompt_backend("local", None, None, None, None, False, assume_yes=False).mode == "local"


def test_yes_remote_falls_back_to_defaults_without_prompting():
    from auryn.cli import _prompt_backend

    b = _prompt_backend("remote", None, None, None, None, False, assume_yes=True)
    assert (b.mode, b.host, b.port, b.username, b.password) == ("remote", "localhost", 6379, None, None)


def test_yes_remote_honors_explicit_connection():
    from auryn.cli import _prompt_backend

    b = _prompt_backend("remote", "db.example", 6400, "u", "p", True, assume_yes=True)
    assert (b.mode, b.host, b.port, b.username, b.password, b.ssl) == ("remote", "db.example", 6400, "u", "p", True)


def test_yes_keeps_the_saved_trigger_and_embed_setting(tmp_path, monkeypatch):
    """`install -y` re-run on a configured machine: an option that isn't passed keeps what
    config.toml already says. It used to fall back to `commit` / no embeddings, which silently
    turned a save-triggered repo's watcher and auto-embed off. An explicit option still wins."""
    from click.testing import CliRunner

    from auryn.cli import cli

    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "cg"))
    monkeypatch.setattr("auryn.core.install.default_adapters", lambda: [_FakeAgent()])
    save_config(Config(resync_trigger="save", auto_embed=True))

    assert CliRunner().invoke(cli, ["install", "-y"]).exit_code == 0
    cfg = load_config()
    assert (cfg.resync_trigger, cfg.auto_embed) == ("save", True)

    assert CliRunner().invoke(cli, ["install", "-y", "--trigger", "manual", "--no-embed"]).exit_code == 0
    cfg = load_config()
    assert (cfg.resync_trigger, cfg.auto_embed) == ("manual", False)


# ── File-config agents (Antigravity / Copilot / Cursor / Windsurf) ────────────
def test_mcp_config_agent_register_and_deregister(tmp_path):
    from auryn.core.agents import McpConfigAgent

    app = tmp_path / ".cursor"
    cfg = app / "mcp.json"
    agent = McpConfigAgent("cursor", app, cfg)

    reg = agent.register(["cairn", "mcp"])
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["auryn"] == {"command": "cairn", "args": ["mcp"]}
    assert reg.name == "cursor" and reg.config_path == str(cfg)

    agent.deregister(reg)
    assert "auryn" not in json.loads(cfg.read_text()).get("mcpServers", {})


def test_mcp_config_agent_preserves_other_entries(tmp_path):
    from auryn.core.agents import McpConfigAgent

    app = tmp_path / ".gemini"
    app.mkdir()
    cfg = app / "config" / "mcp_config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}))
    agent = McpConfigAgent("antigravity", app, cfg)

    reg = agent.register(["cairn", "mcp"])
    data = json.loads(cfg.read_text())
    assert set(data["mcpServers"]) == {"other", "auryn"}  # ours added, other kept
    assert data["theme"] == "dark"  # unrelated keys untouched

    agent.deregister(reg)
    data = json.loads(cfg.read_text())
    assert set(data["mcpServers"]) == {"other"}  # only ours removed
    assert data["theme"] == "dark"


def test_mcp_config_agent_detects_by_app_dir(tmp_path):
    from auryn.core.agents import McpConfigAgent

    app = tmp_path / ".copilot"
    agent = McpConfigAgent("github-copilot", app, app / "mcp-config.json")
    assert agent.detect() is False
    app.mkdir()
    assert agent.detect() is True


def test_mcp_config_agent_refuses_to_clobber_invalid_json(tmp_path):
    from auryn.core.agents import McpConfigAgent

    cfg = tmp_path / "mcp.json"
    cfg.write_text('{ "mcpServers": {}, // a comment makes this JSONC, not JSON\n}')
    agent = McpConfigAgent("cursor", tmp_path, cfg)

    with pytest.raises(RuntimeError, match="not valid JSON"):
        agent.register(["cairn", "mcp"])
    assert "// a comment" in cfg.read_text()  # left untouched


def test_default_adapters_cover_the_agents():
    from auryn.core.agents import default_adapters

    names = {a.name for a in default_adapters()}
    assert {
        "claude-code",
        "codex",
        "antigravity",
        "github-copilot",
        "cursor",
        "windsurf",
        "gemini-cli",
        "vscode",
        "agents-md",
        "aider",
    } <= names


def test_gemini_cli_adapter_targets_settings_json(tmp_path, monkeypatch):
    # Lock the Gemini CLI integration to its real layout: ~/.gemini/settings.json, standard
    # `mcpServers` map. Distinct file from antigravity's config/mcp_config.json under the same home,
    # so registering both is safe and merging preserves the user's other settings.json keys.
    from auryn.core import agents

    monkeypatch.setattr(agents, "_home", lambda: tmp_path)
    adapter = agents.gemini_cli_adapter()
    cfg = tmp_path / ".gemini" / "settings.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"theme": "GitHub", "mcpServers": {"other": {"command": "x"}}}))

    assert adapter.name == "gemini-cli"
    assert adapter.config_path == cfg
    reg = adapter.register(["cairn", "mcp"])
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["auryn"] == {"command": "cairn", "args": ["mcp"]}
    assert set(data["mcpServers"]) == {"other", "auryn"} and data["theme"] == "GitHub"

    adapter.deregister(reg)
    assert set(json.loads(cfg.read_text())["mcpServers"]) == {"other"}


def test_antigravity_adapter_targets_verified_config(tmp_path, monkeypatch):
    # Locks the Antigravity integration to its verified layout (Antigravity docs, Nov 2025):
    # global config at ~/.gemini/config/mcp_config.json, standard `mcpServers` map, local server
    # as {command, args}. Guards against a silent drift back to a wrong path/schema.
    from auryn.core import agents

    monkeypatch.setattr(agents, "_home", lambda: tmp_path)
    a = agents.antigravity_adapter()
    assert a.config_path == tmp_path / ".gemini" / "config" / "mcp_config.json"
    assert a.app_dir == tmp_path / ".gemini"

    reg = a.register(["auryn", "mcp"])
    data = json.loads(a.config_path.read_text())
    assert data["mcpServers"]["auryn"] == {"command": "auryn", "args": ["mcp"]}
    assert reg.name == "antigravity" and reg.config_path == str(a.config_path)


def test_vscode_adapter_targets_user_mcp_json(tmp_path, monkeypatch):
    # Lock the VS Code Copilot Chat integration to its verified layout (VS Code MCP configuration
    # reference, 2026): USER-scoped mcp.json in the profile folder, top-level `servers` map (NOT
    # `mcpServers`), local server as {type: "stdio", command, args}. Guards against drift back to
    # the wrong key/schema and confirms unrelated content is preserved.
    from auryn.core import agents

    monkeypatch.setattr(agents, "_home", lambda: tmp_path)
    adapter = agents.vscode_adapter()
    user = tmp_path / "Library" / "Application Support" / "Code" / "User"
    cfg = user / "mcp.json"

    assert adapter.name == "vscode"
    assert adapter.app_dir == user and adapter.config_path == cfg
    assert adapter.detect() is False  # VS Code not "installed" yet

    user.mkdir(parents=True)
    cfg.write_text(json.dumps({"servers": {"other": {"type": "stdio", "command": "x"}}, "inputs": [{"id": "k"}]}))
    assert adapter.detect() is True  # profile dir exists

    reg = adapter.register(["cairn", "mcp"])
    data = json.loads(cfg.read_text())
    assert "mcpServers" not in data  # VS Code uses `servers`, not `mcpServers`
    assert data["servers"]["auryn"] == {"type": "stdio", "command": "cairn", "args": ["mcp"]}
    assert set(data["servers"]) == {"other", "auryn"}  # ours added, other kept
    assert data["inputs"] == [{"id": "k"}]  # unrelated top-level key untouched
    assert reg.name == "vscode" and reg.config_path == str(cfg)

    adapter.deregister(reg)
    data = json.loads(cfg.read_text())
    assert set(data["servers"]) == {"other"} and data["inputs"] == [{"id": "k"}]  # only ours removed


def test_agents_md_adapter_merges_idempotently_and_preserves_text(tmp_path, monkeypatch):
    # Generic AGENTS.md target: merge a delimited `auryn` section into ~/AGENTS.md, idempotent
    # on re-run, never clobbering surrounding text. Detection is always-True (universal fallback).
    from auryn.core import agents

    monkeypatch.setattr(agents, "_home", lambda: tmp_path)
    adapter = agents.agents_md_adapter()
    md = tmp_path / "AGENTS.md"

    assert adapter.name == "agents-md" and adapter.path == md
    assert adapter.detect() is True  # generic fallback: applies everywhere

    md.write_text("# House rules\n\nAlways write tests.\n")
    reg = adapter.register(["cairn", "mcp"])
    text = md.read_text()
    assert "# House rules" in text and "Always write tests." in text  # surrounding text preserved
    assert "auryn" in text and "cairn mcp" in text  # our section + the server command
    assert text.count(agents._AGENTS_MD_BEGIN) == 1 and text.count(agents._AGENTS_MD_END) == 1

    adapter.register(["cairn", "mcp"])  # re-run
    assert md.read_text() == text  # idempotent: replaced in place, not duplicated
    assert md.read_text().count(agents._AGENTS_MD_BEGIN) == 1

    adapter.deregister(reg)
    final = md.read_text()
    assert "# House rules" in final and "Always write tests." in final  # user's text survives
    assert agents._AGENTS_MD_BEGIN not in final and "auryn" not in final  # only our section gone


def test_aider_adapter_writes_conventions_gated_on_cli(tmp_path, monkeypatch):
    # Aider has no stable native MCP config, so we serve it via its Markdown conventions file
    # (~/CONVENTIONS.md, loaded with `aider --read`). Detection is gated on the `aider` CLI so we
    # don't drop a file for users without Aider; deregister removes a file that held only our section.
    from auryn.core import agents

    monkeypatch.setattr(agents, "_home", lambda: tmp_path)
    monkeypatch.setattr(agents.shutil, "which", lambda name: None)
    adapter = agents.aider_adapter()
    conv = tmp_path / "CONVENTIONS.md"

    assert adapter.name == "aider" and adapter.path == conv
    assert adapter.detect() is False  # aider CLI absent
    monkeypatch.setattr(agents.shutil, "which", lambda name: "/usr/local/bin/aider" if name == "aider" else None)
    assert adapter.detect() is True

    reg = adapter.register(["cairn", "mcp"])
    text = conv.read_text()
    assert agents._AGENTS_MD_BEGIN in text and "auryn" in text and "cairn mcp" in text
    assert reg.name == "aider" and reg.config_path == str(conv)

    adapter.deregister(reg)
    assert not conv.exists()  # file held only our section (we created it) -> removed


def test_agents_md_deregister_keeps_file_when_it_predated_us(tmp_path):
    # If the AGENTS.md already existed, deregister must strip only our section and keep the file.
    from auryn.core.agents import AgentsMdAgent

    md = tmp_path / "AGENTS.md"
    md.write_text("# Preexisting\n")
    agent = AgentsMdAgent("agents-md", md)
    reg = agent.register(["cairn", "mcp"])
    agent.deregister(reg)
    assert md.exists() and md.read_text() == "# Preexisting\n"


def test_do_install_skips_a_failing_adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "cg"))
    from auryn.core.app_config import Backend, load_config
    from auryn.core.install import do_install

    class _Boom:
        name = "boom"

        def detect(self):
            return True

        def register(self, cmd):
            raise RuntimeError("nope")

    good = _FakeAgent()
    cfg = do_install(Backend(mode="local"), adapters=[_Boom(), good], server_command=["x"])
    assert [a.name for a in cfg.agents] == ["fake"]  # only the good one survived
    assert [a.name for a in load_config().agents] == ["fake"]
