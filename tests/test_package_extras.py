import re
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _distribution_name(requirement: str) -> str:
    match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
    assert match
    return re.sub(r"[-_.]+", "-", match.group().lower())


def _project(path: Path) -> dict:
    return tomllib.loads(path.read_text())["project"]


def test_default_install_requires_only_core():
    project = _project(ROOT / "pyproject.toml")

    assert [_distribution_name(requirement) for requirement in project["dependencies"]] == [
        "auryn-core"
    ]


def test_each_extension_is_available_as_an_extra_and_in_all():
    project = _project(ROOT / "pyproject.toml")
    optional_dependencies = project["optional-dependencies"]
    extension_names = {
        _distribution_name(_project(path)["name"])
        for path in (ROOT / "packages").glob("*/pyproject.toml")
        if _project(path)["name"] != "auryn-core"
    }

    for extension in extension_names:
        extra = extension.removeprefix("auryn-")
        assert _distribution_name(optional_dependencies[extra][0]) == extension

    all_extensions = {
        _distribution_name(requirement)
        for requirement in optional_dependencies["all"]
    }
    assert all_extensions == extension_names
    assert all(
        _distribution_name(requirement) != "falkordb-auryn"
        for requirements in optional_dependencies.values()
        for requirement in requirements
    )
