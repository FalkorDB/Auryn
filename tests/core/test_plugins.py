"""On-demand plugin catalog + auto-install wiring (core/plugins.py, core/ingest.py).

Offline: the actual `pip install` (plugins.install_plugin) is monkeypatched, so these never
touch the network. They cover the catalog mapping, the opt-OUT resolution, and that
_ingest_from_source's auto-install helper installs exactly the missing catalog packages and
re-discovers -- and is a strict no-op when auto-install is disabled or nothing is missing."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from auryn.core import ingest, plugins
from auryn.core.registry import GitLanguage


def test_packages_for_suffixes_dedup():
    # .c and .h both map to auryn-c -> one package; a suffix no plugin claims isn't in the catalog.
    assert set(plugins.packages_for_suffixes({".c", ".h", ".go"})) == {"auryn-c", "auryn-go"}
    assert plugins.packages_for_suffixes({".md"}) == {}


def test_catalog_covers_bundled_languages_and_has_no_phantom_packages():
    # Regression (release B4): the languages that appear in real repos / the bundled quickstart
    # sample must be catalogued, so a lean `[embedded]` install + `auryn quickstart` auto-pulls
    # their ingestor; and every catalog target must be a REAL published dist (the old catalog
    # mapped .js/.jsx to a nonexistent `auryn-javascript`).
    import pathlib

    pp = plugins.PLUGIN_PACKAGES
    assert pp.get(".py") == "auryn-python"          # the quickstart sample is Python
    assert pp.get(".kt") == "auryn-kotlin"
    assert pp.get(".swift") == "auryn-swift"
    assert pp.get(".sh") == "auryn-bash"
    assert pp.get(".js") == "auryn-typescript" and pp.get(".jsx") == "auryn-typescript"  # TS owns JS
    assert "auryn-javascript" not in set(pp.values())
    # every catalog target maps to a real packages/<lang>/ workspace member (auryn-<dir>)
    pkg_root = pathlib.Path(__file__).resolve().parents[2] / "packages"
    members = {f"auryn-{p.name}" for p in pkg_root.iterdir() if p.is_dir()}
    phantom = set(pp.values()) - members
    assert not phantom, f"catalog references non-existent packages: {sorted(phantom)}"


def test_csharp_is_catalogued():
    # .cs must be in the catalog so ingesting C# on a lean install auto-pulls auryn-csharp
    # (which carries the bundled Roslyn extractor) with no user interaction -- parity with .java etc.
    assert plugins.PLUGIN_PACKAGES.get(".cs") == "auryn-csharp"
    assert plugins.packages_for_suffixes({".cs"}) == {"auryn-csharp": ".cs"}


def test_autoinstall_pulls_csharp_for_a_cs_source(monkeypatch):
    installed: list[str] = []
    monkeypatch.setattr(plugins, "install_plugin", lambda pkg: installed.append(pkg) or True)
    monkeypatch.setattr(ingest, "discover_git_languages", lambda: {"csharp": _lang("csharp", (".cs",))})

    langs, returned = ingest._autoinstall_missing(_FakeSource({".cs"}), "sha", {}, auto_install=True)

    assert installed == ["auryn-csharp"] and returned == ["auryn-csharp"] and "csharp" in langs


def test_auto_install_is_opt_out(monkeypatch):
    monkeypatch.delenv("AURYN_AUTO_INSTALL", raising=False)
    assert plugins.auto_install_enabled(None) is True  # default ON (opt-out)
    assert plugins.auto_install_enabled(False) is False  # explicit off wins
    monkeypatch.setenv("AURYN_AUTO_INSTALL", "0")
    assert plugins.auto_install_enabled(None) is False  # env turns it off
    assert plugins.auto_install_enabled(True) is True  # explicit on wins over env


class _FakeSource:
    """Duck-typed source: reports files present only for the given suffixes."""

    def __init__(self, present: set[str]):
        self.present = present

    def list_files(self, sha, suffix, excluded):
        return [("file" + suffix, "hash")] if suffix in self.present else []


def _lang(name, suffixes):
    return GitLanguage(name, suffixes, frozenset(), lambda *a, **k: None)


def test_autoinstall_installs_missing_and_rediscovers(monkeypatch):
    installed: list[str] = []
    monkeypatch.setattr(plugins, "install_plugin", lambda pkg: installed.append(pkg) or True)
    # after the "install", discovery now returns a go language
    monkeypatch.setattr(ingest, "discover_git_languages", lambda: {"go": _lang("go", (".go",))})

    langs, returned = ingest._autoinstall_missing(_FakeSource({".go"}), "sha", {}, auto_install=True)

    assert installed == ["auryn-go"]
    assert returned == ["auryn-go"]
    assert "go" in langs


def test_autoinstall_disabled_is_a_noop(monkeypatch):
    monkeypatch.setattr(plugins, "install_plugin", lambda pkg: pytest.fail("must not install when disabled"))
    langs = {"python": _lang("python", (".py",))}
    out, installed = ingest._autoinstall_missing(_FakeSource({".go"}), "sha", langs, auto_install=False)
    assert out is langs and installed == []


def test_autoinstall_skips_already_claimed(monkeypatch):
    monkeypatch.setattr(plugins, "install_plugin", lambda pkg: pytest.fail("should not install a claimed suffix"))
    langs = {"c": _lang("c", (".c", ".h"))}  # a plugin already claims .c/.h
    out, installed = ingest._autoinstall_missing(_FakeSource({".c"}), "sha", langs, auto_install=True)
    assert out is langs and installed == []


# -- _install_command: target the RUNNING interpreter -----------------------------------

def test_install_command_uv_targets_running_interpreter_in_a_venv(monkeypatch):
    # uv available + a venv (prefix != base_prefix): `uv pip install --python <exe>`, NO --system,
    # so a runtime auto-install lands in THIS interpreter's env (incl. a uv-tool venv), not elsewhere.
    monkeypatch.setattr(plugins.shutil, "which", lambda name: "/usr/bin/uv")
    monkeypatch.setattr(plugins.sys, "prefix", "/venv")
    monkeypatch.setattr(plugins.sys, "base_prefix", "/usr")
    cmd = plugins._install_command("pkg")
    assert cmd[:3] == ["uv", "pip", "install"]
    assert "--python" in cmd and plugins.sys.executable in cmd
    assert "--system" not in cmd
    assert cmd[-1] == "pkg"


def test_install_command_uv_adds_system_for_a_non_venv_interpreter(monkeypatch):
    # uv available + a system/global interpreter (prefix == base_prefix): uv refuses without --system.
    monkeypatch.setattr(plugins.shutil, "which", lambda name: "/usr/bin/uv")
    monkeypatch.setattr(plugins.sys, "prefix", "/usr")
    monkeypatch.setattr(plugins.sys, "base_prefix", "/usr")
    assert "--system" in plugins._install_command("pkg")


def test_install_command_pip_fallback_targets_sys_executable(monkeypatch):
    # no uv: fall back to the interpreter's own pip, which always targets the running interpreter.
    monkeypatch.setattr(plugins.shutil, "which", lambda name: None)
    cmd = plugins._install_command("pkg")
    assert cmd == [plugins.sys.executable, "-m", "pip", "install", "pkg"]


class _FakePip:
    """Records each install command and the constraints file it was given; fails specs in `fail`."""

    def __init__(self, fail=()):
        self.fail, self.calls = set(fail), []

    def __call__(self, cmd, **kwargs):
        constraints = Path(cmd[cmd.index("-c") + 1]).read_text() if "-c" in cmd else None
        spec = cmd[cmd.index("-c") - 1] if "-c" in cmd else cmd[-1]
        self.calls.append((spec, constraints))
        return subprocess.CompletedProcess(cmd, 1 if spec in self.fail else 0, "", "")


def test_install_plugin_tries_the_core_version_and_holds_installed_auryn_packages(monkeypatch):
    # Unpinned, `pip install auryn-cuda` pulls the newest cuda, whose `auryn-cpp==` pin would
    # upgrade an auryn-cpp this process has already imported. Installed auryn-* stay where they are.
    pip = _FakePip()
    monkeypatch.setattr(plugins.subprocess, "run", pip)
    installed = [_dist("auryn-core", "9.1"), _dist("auryn-cpp", "9.0"), _dist("jedi", "1")]
    monkeypatch.setattr(plugins.importlib.metadata, "distributions", lambda: installed)

    assert plugins.install_plugin("auryn-cuda")
    assert plugins.install_plugin("watchdog>=4", label="dependency")

    assert pip.calls == [
        ("auryn-cuda==9.1", "auryn-core==9.1\nauryn-cpp==9.0\n"),
        ("watchdog>=4", None),
    ]


def test_install_plugin_falls_back_to_the_newest_version_that_fits(monkeypatch, caplog):
    # The core's version of the plugin may not be on PyPI (0.8.0 has auryn-core but no auryn-python),
    # or may not fit the held packages (a core upgraded on its own, newer than auryn-cpp).
    pip = _FakePip(fail={"auryn-cuda==9.1"})
    monkeypatch.setattr(plugins.subprocess, "run", pip)
    installed = [_dist("auryn-core", "9.1"), _dist("auryn-cpp", "9.0")]
    monkeypatch.setattr(plugins.importlib.metadata, "distributions", lambda: installed)

    with caplog.at_level(logging.INFO, logger="auryn.plugins"):
        assert plugins.install_plugin("auryn-cuda")

    assert [spec for spec, _ in pip.calls] == ["auryn-cuda==9.1", "auryn-cuda"]
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]  # the fallback worked: no error
    assert pip.calls[1][1] == "auryn-core==9.1\nauryn-cpp==9.0\n"


def _dist(name, version):
    return SimpleNamespace(metadata={"Name": name}, version=version)


# -- ensure_importable: on-demand optional-dependency install --------------------------

def test_ensure_importable_returns_already_present_module():
    # a stdlib module is always importable -> returned without any install attempt.
    import json as json_mod

    assert plugins.ensure_importable("json") is json_mod


def test_ensure_importable_raises_actionable_error_when_missing_and_disabled(monkeypatch):
    monkeypatch.setattr(plugins, "install_plugin", lambda *a, **k: pytest.fail("must not install when disabled"))
    with pytest.raises(ImportError) as excinfo:
        plugins.ensure_importable("no_such_driver_xyz", "no-such-driver>=1.0", auto_install=False)
    message = str(excinfo.value)
    assert "no_such_driver_xyz" in message
    assert "no-such-driver>=1.0" in message  # tells the user exactly what to pip install
    assert "AURYN_AUTO_INSTALL" in message


def test_ensure_importable_auto_installs_then_imports(monkeypatch):
    import sys
    import types

    installed: list[tuple[str, str]] = []

    def fake_install(package, label="plugin"):
        installed.append((package, label))
        sys.modules["fake_driver_pkg"] = types.ModuleType("fake_driver_pkg")  # simulate the install
        return True

    monkeypatch.setattr(plugins, "install_plugin", fake_install)
    try:
        module = plugins.ensure_importable("fake_driver_pkg", "fake-driver>=2.0", auto_install=True)
        assert module is sys.modules["fake_driver_pkg"]
        assert installed == [("fake-driver>=2.0", "dependency")]  # spec + label passed through
    finally:
        sys.modules.pop("fake_driver_pkg", None)


def test_ensure_importable_raises_when_install_fails(monkeypatch):
    monkeypatch.setattr(plugins, "install_plugin", lambda *a, **k: False)  # install "fails"
    with pytest.raises(ImportError, match="Auto-install of 'still-missing"):
        plugins.ensure_importable("still_missing_mod", "still-missing>=1", auto_install=True)
