"""The Claude Code plugin in claude-plugin/ and the marketplace that lists it.

- The plugin's version and the falkordb-auryn version its server runs are the same release.
- The server is pinned to an exact version (the directory blocks an unpinned uvx launcher).
- The plugin's skill is the skill `auryn install` copies.
- The marketplace entry points at the plugin folder, by the same name.
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "claude-plugin"
PIN = re.compile(r"^falkordb-auryn(?:\[[a-z,-]+\])?==(?P<version>\d+\.\d+\.\d+)$")


def _json(path: Path) -> dict:
    return json.loads(path.read_text())


def _server() -> dict:
    return _json(PLUGIN / ".mcp.json")["mcpServers"]["auryn"]


def _pinned_version() -> str:
    args = _server()["args"]
    spec = args[args.index("--from") + 1]
    match = PIN.match(spec)
    assert match, f"the server must run an exact falkordb-auryn version, got {spec!r}"
    return match["version"]


def test_plugin_version_is_the_pinned_release():
    assert _json(PLUGIN / ".claude-plugin" / "plugin.json")["version"] == _pinned_version()


def test_server_runs_the_auryn_mcp_command():
    server = _server()
    assert server["command"] == "uvx"
    assert server["args"][-2:] == ["auryn", "mcp"]


def test_plugin_skill_matches_the_packaged_skill():
    packaged = ROOT / "packages" / "core" / "src" / "auryn" / "assets" / "skills" / "auryn"
    plugin = PLUGIN / "skills" / "auryn"
    assert sorted(p.name for p in plugin.iterdir()) == sorted(p.name for p in packaged.iterdir())
    for path in packaged.iterdir():
        assert (plugin / path.name).read_bytes() == path.read_bytes(), f"copy {path} to {plugin}"


def test_marketplace_lists_the_plugin():
    (entry,) = _json(ROOT / ".claude-plugin" / "marketplace.json")["plugins"]
    assert entry["name"] == _json(PLUGIN / ".claude-plugin" / "plugin.json")["name"]
    assert (ROOT / entry["source"]).resolve() == PLUGIN
