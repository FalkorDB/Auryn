"""Check an installed Auryn selection, run with the interpreter of the environment under test.

Usage: python check_install.py SPEC VERSION REPO_ROOT
  SPEC: what was installed, e.g. `falkordb-auryn`, `auryn-core[python,http]`, `falkordb-auryn[all]`.

Asserts that exactly the expected auryn-* packages are installed (what SPEC names, plus the
auryn-* dependencies of those, read from the workspace's pyproject files), all at VERSION (the
locally built wheels, not PyPI), and that the CLI and every plugin entry point load.
"""

import importlib.metadata
import pathlib
import re
import sys
import tomllib

spec, version, root = sys.argv[1], sys.argv[2], pathlib.Path(sys.argv[3])
normalize = lambda name: re.sub(r"[-_.]+", "-", name).lower()
dist_name = lambda requirement: normalize(re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement).group())


def project(path: pathlib.Path) -> dict:
    return tomllib.loads(path.read_text())["project"]


projects = {normalize(p["name"]): p for p in map(project, [root / "pyproject.toml", *root.glob("packages/*/pyproject.toml")])}

name, _, extras = spec.partition("[")
name, extras = normalize(name), [e for e in extras.rstrip("]").split(",") if e]
wanted = [name] + [r for e in extras for r in projects[name]["optional-dependencies"][e]]

expected: set[str] = set()
todo = [dist_name(r) for r in wanted]
while todo:
    dist = todo.pop()
    if dist in expected or dist not in projects:
        continue
    expected.add(dist)
    todo += [dist_name(r) for r in projects[dist].get("dependencies", [])]

installed = {normalize(d.metadata["Name"]): d.version for d in importlib.metadata.distributions()}
# Every auryn distribution, not only workspace ones, so a stray auryn-* package also fails the check.
found = {d: v for d, v in installed.items() if d in projects or d.startswith("auryn-")}
assert set(found) == expected, f"expected {sorted(expected)}, installed {sorted(found)}"
stale = {d: v for d, v in found.items() if v != version}
assert not stale, f"not the locally built version {version}: {stale}"

import auryn.cli  # noqa: E402,F401

loaded = 0
for group in ("auryn.languages", "auryn.ingestors", "auryn.enrichments"):
    for entry_point in importlib.metadata.entry_points(group=group):
        entry_point.load()
        loaded += 1
print(f"{spec}: {len(found)} auryn packages at {version}, {loaded} entry points loaded")
