"""install/uninstall orchestration (with a fake agent adapter) and the Claude Code adapter's
register/deregister (with a fake command runner + temp skills dir). No real `claude` CLI, no
touching a real agent config."""

from __future__ import annotations

import json
import subprocess

import pytest

from auryn.core.app_config import AgentRegistration, Backend, load_config
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

    cc = ClaudeCode(run=run, skills_dir=tmp_path / "skills", config_path=tmp_path / "claude.json")
    reg = cc.register(["PY", "-m", "auryn", "mcp"])

    assert ["claude", "mcp", "add", "-s", "user", "auryn", "--", "PY", "-m", "auryn", "mcp"] in ran
    assert (tmp_path / "skills" / "auryn" / "SKILL.md").exists()  # bundled skill copied
    assert reg.skill_path == str(tmp_path / "skills" / "auryn")

    cc.deregister(reg)
    assert ["claude", "mcp", "remove", "-s", "user", "auryn"] in ran
    assert not (tmp_path / "skills" / "auryn").exists()  # skill removed


def test_claudecode_deregister_leaves_the_users_own_files_in_the_skill_dir(tmp_path):
    # Uninstall removes what install copied -- not the whole folder: a file the user added next
    # to the bundled skill (a note, their own variant) is theirs and must survive.
    from auryn.core.agents import ClaudeCode

    skill = tmp_path / "skills" / "auryn"
    skill.mkdir(parents=True)
    (skill / "MINE.md").write_text("mine")

    cc = ClaudeCode(run=_ok, skills_dir=tmp_path / "skills")
    reg = cc.register(["PY", "-m", "auryn", "mcp"])
    assert (skill / "SKILL.md").exists()

    cc.deregister(reg)
    assert not (skill / "SKILL.md").exists()  # ours: gone
    assert (skill / "MINE.md").read_text() == "mine"  # theirs: untouched


def test_claudecode_never_reaches_through_a_symlinked_skill_dir(tmp_path):
    # After install the user replaces the skill folder with a link to a skill they maintain
    # elsewhere. Its SKILL.md has the bundled file's name but is theirs: a re-install must not
    # copy the bundled skill over it through the link (it still registers the MCP server), and
    # uninstall must not follow the link and delete it.
    from auryn.core.agents import ClaudeCode

    cc = ClaudeCode(run=_ok, skills_dir=tmp_path / "skills")
    reg = cc.register(["PY", "-m", "auryn", "mcp"])

    theirs = tmp_path / "my-skill"
    theirs.mkdir()
    (theirs / "SKILL.md").write_text("mine")
    skill = tmp_path / "skills" / "auryn"
    (skill / "SKILL.md").unlink()
    skill.rmdir()
    skill.symlink_to(theirs, target_is_directory=True)

    reinstalled = cc.register(["PY", "-m", "auryn", "mcp"])
    assert reinstalled.skill_path is None and reinstalled.server_name == "auryn"
    assert [p.name for p in theirs.iterdir()] == ["SKILL.md"]
    assert (theirs / "SKILL.md").read_text() == "mine"

    cc.deregister(reg)
    cc.deregister(reinstalled)
    assert (theirs / "SKILL.md").read_text() == "mine"


@pytest.mark.parametrize("denied_call", ["unlink", "is_symlink"])
def test_claudecode_deregister_carries_on_past_a_file_it_cannot_remove(tmp_path, monkeypatch, denied_call):
    # Best-effort, as the rmtree(ignore_errors=True) it replaced: an unremovable skill file -- or a
    # skills folder it cannot even look into -- must not raise out of deregister and strand the
    # agents and the manifest after it.
    from pathlib import Path

    from auryn.core.agents import ClaudeCode

    cc = ClaudeCode(run=_ok, skills_dir=tmp_path / "skills")
    reg = cc.register(["PY", "-m", "auryn", "mcp"])

    def denied(self, *a, **k):
        raise PermissionError("read-only")

    monkeypatch.setattr(Path, denied_call, denied)
    cc.deregister(reg)  # does not raise
    assert (tmp_path / "skills" / "auryn" / "SKILL.md").exists()  # and leaves what it couldn't check


def test_claudecode_register_sets_always_load(tmp_path):
    # `claude mcp add` can't set alwaysLoad, so register() patches the config it wrote so the
    # server's tools load into the agent's INITIAL tool list instead of being deferred.
    from auryn.core.agents import ClaudeCode

    cfg = tmp_path / "claude.json"
    cfg.write_text(json.dumps({"mcpServers": {"auryn": {"type": "stdio", "command": "PY", "args": ["-m", "auryn", "mcp"]}}}))

    cc = ClaudeCode(run=_ok, skills_dir=tmp_path / "skills", config_path=cfg)
    cc.register(["PY", "-m", "auryn", "mcp"])

    entry = json.loads(cfg.read_text())["mcpServers"]["auryn"]
    assert entry["alwaysLoad"] is True  # tools surfaced upfront, not hidden behind tool-search
    assert entry["command"] == "PY" and entry["args"] == ["-m", "auryn", "mcp"]  # other fields preserved


def test_claudecode_register_always_load_is_best_effort(tmp_path):
    # No config file yet (or an unwritten one) -> register still succeeds; the server just stays
    # deferred rather than erroring. Also must not clobber an unparseable config.
    from auryn.core.agents import ClaudeCode

    missing = tmp_path / "nope.json"
    reg = ClaudeCode(run=_ok, skills_dir=tmp_path / "skills", config_path=missing).register(["PY", "-m", "auryn", "mcp"])
    assert reg.skill_path and not missing.exists()  # didn't raise, didn't create a bogus file

    junk = tmp_path / "junk.json"
    junk.write_text("{not json")
    ClaudeCode(run=_ok, skills_dir=tmp_path / "skills2", config_path=junk).register(["PY", "-m", "auryn", "mcp"])
    assert junk.read_text() == "{not json"  # left untouched


def test_claudecode_always_load_write_failure_is_non_fatal(tmp_path, monkeypatch):
    # Patching the config is strictly best-effort: if the write raises (read-only / raced fs), the
    # agent must still register (server stays, tools just deferred) rather than aborting install.
    from auryn.core import agents

    cfg = tmp_path / "claude.json"
    cfg.write_text(json.dumps({"mcpServers": {"auryn": {"command": "PY"}}}))

    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise OSError("read-only fs")

    monkeypatch.setattr(agents, "_write_config", boom)
    reg = agents.ClaudeCode(run=_ok, skills_dir=tmp_path / "skills", config_path=cfg).register(["PY", "-m", "auryn", "mcp"])
    assert calls, "the alwaysLoad write must have actually been attempted (failure path exercised)"
    assert reg.skill_path  # ...yet register still completed despite the write failure


def test_claudecode_always_load_skips_when_config_changed_mid_patch(tmp_path, monkeypatch):
    # Claude Code also writes ~/.claude.json during sessions, and the alwaysLoad patch rewrites the
    # WHOLE file. If it changes between our snapshot read and the write, skip rather than clobber the
    # concurrent writer (the server just stays deferred). Narrows the read-modify-write race.
    from auryn.core import agents

    cfg = tmp_path / "claude.json"
    snapshot = json.dumps({"mcpServers": {"auryn": {"command": "PY"}}})
    cfg.write_text(snapshot)
    changed = json.dumps({"mcpServers": {"auryn": {"command": "PY"}, "other": {"command": "X"}}})

    # Snapshot read returns the original; the re-read just before writing sees a concurrent change.
    seq = [snapshot, changed]
    monkeypatch.setattr(agents.Path, "read_text", lambda self, *a, **k: seq.pop(0) if seq else changed)
    writes = []
    monkeypatch.setattr(agents, "_write_config", lambda p, d: writes.append(d))

    agents.ClaudeCode(run=_ok, skills_dir=tmp_path / "skills", config_path=cfg)._enable_always_load()
    assert not writes, "the whole-file rewrite must be skipped when the config changed under us"


def test_claudecode_config_path_honors_claude_config_dir(tmp_path, monkeypatch):
    # `claude mcp add` writes to $CLAUDE_CONFIG_DIR/.claude.json when that env is set, so the
    # alwaysLoad patch must target the same file -- not ~/.claude.json (a possibly-unrelated profile).
    from pathlib import Path

    from auryn.core.agents import ClaudeCode

    monkeypatch.delenv("AURYN_CLAUDE_CONFIG", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfgdir"))
    assert ClaudeCode()._claude_config() == tmp_path / "cfgdir" / ".claude.json"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert ClaudeCode()._claude_config() == Path.home() / ".claude.json"  # falls back to home dir


def test_write_config_preserves_mode_and_is_clean(tmp_path):
    # Security: replacing a 0600 config (may hold MCP env/secrets) must not widen it; a new file is
    # created owner-only; and no stray temp files are left behind.
    import os
    import stat
    from auryn.core.agents import _write_config

    # The mode-bit assertions are POSIX-only: native Windows chmod honors just the read-only bit, so
    # S_IMODE reports a writable file as 0o666, not 0o600. Gate them by platform (the file content /
    # no-stray-temp checks still run everywhere); Windows access control would be a separate check.
    posix = os.name == "posix"

    existing = tmp_path / "cfg.json"
    existing.write_text("{}")
    os.chmod(existing, 0o600)
    _write_config(existing, {"mcpServers": {"auryn": {"alwaysLoad": True}}})
    if posix:
        assert stat.S_IMODE(existing.stat().st_mode) == 0o600
    assert json.loads(existing.read_text())["mcpServers"]["auryn"]["alwaysLoad"] is True

    new = tmp_path / "new.json"
    _write_config(new, {"x": 1})
    if posix:
        assert stat.S_IMODE(new.stat().st_mode) == 0o600
    assert not list(tmp_path.glob("*.tmp"))  # no leftover temp files


def test_write_config_follows_symlink(tmp_path):
    # A symlinked config (e.g. managed dotfiles) must be updated at its target, not have the link
    # replaced by a regular file -- which a naive atomic rename would do.
    from auryn.core.agents import _write_config

    real = tmp_path / "real" / "claude.json"
    real.parent.mkdir()
    real.write_text("{}")
    link = tmp_path / "claude.json"
    link.symlink_to(real)

    _write_config(link, {"ok": True})

    assert link.is_symlink()  # link preserved
    assert json.loads(real.read_text())["ok"] is True  # the target received the update


def test_claudecode_register_raises_when_add_fails(tmp_path):
    from auryn.core.agents import ClaudeCode

    def run(cmd):
        rc = 1 if "add" in cmd else 0  # `remove` (idempotent clear) ok, `add` fails
        return subprocess.CompletedProcess(cmd, rc, "", "boom")

    cc = ClaudeCode(run=run, skills_dir=tmp_path / "skills", config_path=tmp_path / "claude.json")
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


@pytest.mark.parametrize("content", ['[{"theirs": 1}]', '{"mcpServers": [{"name": "theirs"}], "note": 1}'])
def test_mcp_config_agent_refuses_to_clobber_a_config_of_another_shape(tmp_path, content):
    # Valid JSON, but not the `{"mcpServers": {...}}` object we know how to merge one entry into:
    # it used to be replaced wholesale by a config holding only our server.
    from auryn.core.agents import McpConfigAgent

    cfg = tmp_path / "mcp.json"
    cfg.write_text(content)
    agent = McpConfigAgent("cursor", tmp_path, cfg)

    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        agent.register(["cairn", "mcp"])
    assert cfg.read_text() == content  # left untouched


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


def test_mcp_config_agent_treats_a_null_servers_map_as_absent(tmp_path):
    # `"mcpServers": null` holds nothing to preserve, unlike a map of another shape: merge into
    # it as if the key weren't there, and keep the rest of the file.
    from auryn.core.agents import McpConfigAgent

    cfg = tmp_path / "mcp.json"
    cfg.write_text('{"mcpServers": null, "note": 1}')
    McpConfigAgent("cursor", tmp_path, cfg).register(["cairn", "mcp"])

    data = json.loads(cfg.read_text())
    assert data["note"] == 1 and data["mcpServers"]["auryn"]["command"] == "cairn"


def test_agents_md_deregister_keeps_an_empty_file_that_predated_us(tmp_path):
    # "Predated us" holds for an EMPTY file too: with only our section left it looked like a file
    # install had created, and was deleted. Also after a re-install in between.
    from auryn.core.agents import AgentsMdAgent

    md = tmp_path / "AGENTS.md"
    md.write_text("")
    agent = AgentsMdAgent("agents-md", md)
    agent.register(["cairn", "mcp"])
    reg = agent.register(["cairn", "mcp"])  # re-run of install
    agent.deregister(reg)
    assert md.exists() and md.read_text() == ""


@pytest.mark.parametrize("editor_added_a_bom", [False, True])
def test_agents_md_deregister_removes_a_file_install_created(tmp_path, monkeypatch, editor_added_a_bom):
    # ...while a file that exists only because install wrote it is removed again -- also after a
    # re-install in between (the block is still the file's first byte), and also when an editor
    # has since put a byte-order mark in front of it. Under a locale that isn't UTF-8 (Windows'
    # cp1252, as patched here) too: the file is read back as the UTF-8 it was written in.
    import io

    from auryn.core.agents import AgentsMdAgent

    monkeypatch.setattr(io, "text_encoding", lambda encoding, stacklevel=2: encoding or "cp1252")
    md = tmp_path / "AGENTS.md"
    agent = AgentsMdAgent("agents-md", md)
    agent.register(["cairn", "mcp"])
    reg = agent.register(["cairn", "mcp"])  # re-run of install
    if editor_added_a_bom:
        md.write_text("\ufeff" + md.read_text(encoding="utf-8"), encoding="utf-8")
    agent.deregister(reg)
    assert not md.exists()


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
