"""The install design, checked on the workspace's pyproject files.

- `pip install falkordb-auryn` installs the core and every plugin.
- `pip install "auryn-core[python,http]"` installs the core and only the plugins named.
- Extras on the umbrella are only the heavy optional parts.
- Every auryn-* pin is `==` the workspace version, so release.yml can stamp them (lockstep).
"""

import importlib.util
import re
import shutil
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UMBRELLA_EXTRAS = {"embedded", "oracle-live", "sql-all", "all"}
# Core extras that are not one-plugin-each.
CORE_OWN_EXTRAS = {"llm", "embedded", "all"}
CORE_DRIVER_EXTRAS = {"oracle-live": "auryn-oracle[live]", "sql-all": "auryn-sql[all]"}


def _name(requirement: str) -> str:
    match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
    assert match, requirement
    return re.sub(r"[-_.]+", "-", match.group().lower())


def _project(path: Path) -> dict:
    return tomllib.loads(path.read_text())["project"]


def _members() -> dict[str, dict]:
    return {_project(p)["name"]: _project(p) for p in sorted((ROOT / "packages").glob("*/pyproject.toml"))}


def _version() -> str:
    return _project(ROOT / "pyproject.toml")["version"]


def _plugins() -> set[str]:
    return set(_members()) - {"auryn-core"}


def test_every_package_shares_the_workspace_version():
    assert {p["version"] for p in _members().values()} == {_version()}


def test_umbrella_installs_core_and_every_plugin():
    deps = _project(ROOT / "pyproject.toml")["dependencies"]
    assert {_name(r) for r in deps} == _plugins() | {"auryn-core"}
    assert sorted(deps) == sorted(f"{name}=={_version()}" for name in _plugins() | {"auryn-core"})


def test_umbrella_extras_are_only_the_heavy_optional_parts():
    extras = _project(ROOT / "pyproject.toml")["optional-dependencies"]
    v = _version()
    assert set(extras) == UMBRELLA_EXTRAS
    assert extras["embedded"] == [f"auryn-core[embedded]=={v}"]
    assert extras["oracle-live"] == [f"auryn-oracle[live]=={v}"]
    assert extras["sql-all"] == [f"auryn-sql[all]=={v}"]
    assert sorted(extras["all"]) == sorted([f"auryn-oracle[live]=={v}", f"auryn-sql[all]=={v}"])


def test_core_has_one_extra_per_plugin():
    extras = _project(ROOT / "packages/core/pyproject.toml")["optional-dependencies"]
    v = _version()
    plugin_extras = set(extras) - CORE_OWN_EXTRAS - set(CORE_DRIVER_EXTRAS)
    assert plugin_extras == {name.removeprefix("auryn-") for name in _plugins()}
    for extra in plugin_extras:
        assert extras[extra] == [f"auryn-{extra}=={v}"]
    for extra, requirement in CORE_DRIVER_EXTRAS.items():
        assert extras[extra] == [f"{requirement}=={v}"]


def test_core_all_is_every_plugin_and_both_drivers():
    # The same set as `falkordb-auryn[all]`, so `[all]` means the same on both packages.
    v = _version()
    core_all = _project(ROOT / "packages/core/pyproject.toml")["optional-dependencies"]["all"]
    drivers = {"auryn-oracle": "auryn-oracle[live]", "auryn-sql": "auryn-sql[all]"}
    assert sorted(core_all) == sorted(f"{drivers.get(name, name)}=={v}" for name in _plugins())


def test_plugin_to_plugin_dependencies_are_pinned_to_the_same_release():
    # A plugin that reuses another (auryn-cuda -> auryn-cpp, auryn-oracle -> auryn-sql) must get
    # that plugin's same release. The dependency on auryn-core stays unpinned on purpose: the
    # runtime plugin auto-install (core/plugins.py) installs a plugin into a running core and must
    # not move the core under it.
    plugins, version = _plugins(), _version()
    for name, project in _members().items():
        for requirement in project.get("dependencies", []):
            dep = _name(requirement)
            if dep in plugins:
                assert requirement.split("#")[0].strip() == f"{dep}=={version}", (name, requirement)


def test_no_plugin_requires_the_umbrella():
    for project in _members().values():
        requirements = project.get("dependencies", []) + [
            r for rs in project.get("optional-dependencies", {}).values() for r in rs
        ]
        assert "falkordb-auryn" not in {_name(r) for r in requirements}


def test_stamp_version_rewrites_versions_and_pins(tmp_path):
    shutil.copy(ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    for path in (ROOT / "packages").glob("*/pyproject.toml"):
        (tmp_path / "packages" / path.parent.name).mkdir(parents=True)
        shutil.copy(path, tmp_path / "packages" / path.parent.name / "pyproject.toml")
    spec = importlib.util.spec_from_file_location("stamp_version", ROOT / ".github/scripts/stamp_version.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    files = module.stamp("9.8.7", tmp_path)

    assert len(files) == len(_members()) + 1
    for f in files:
        project = _project(f)
        assert project["version"] == "9.8.7"
        requirements = project.get("dependencies", []) + [
            r for rs in project.get("optional-dependencies", {}).values() for r in rs
        ]
        for r in requirements:  # read with tomllib, not the stamp's own pattern (it once missed auryn-k8s)
            if _name(r).startswith("auryn-") and "==" in r:
                assert r.endswith("==9.8.7"), (f, r)
    umbrella = _project(tmp_path / "pyproject.toml")
    assert "auryn-python==9.8.7" in umbrella["dependencies"]
    core = _project(tmp_path / "packages/core/pyproject.toml")
    assert core["optional-dependencies"]["python"] == ["auryn-python==9.8.7"]
    assert core["optional-dependencies"]["oracle-live"] == ["auryn-oracle[live]==9.8.7"]
