"""Stamp one version into every package of the workspace: the umbrella and packages/*.

Rewrites every `auryn-*==<x>` pin (the umbrella's dependencies, auryn-core's plugin extras), so a
release stays lockstep: `falkordb-auryn==X` installs exactly the X of every plugin. A package's
own version is not in its pyproject: it comes from git (hatch-vcs), so build the stamped tree
with `SETUPTOOLS_SCM_PRETEND_VERSION` set to the same VERSION.

Usage: python .github/scripts/stamp_version.py VERSION [ROOT]
Used by release.yml (the release version) and tests.yml (a local-only version for install tests).
"""

import pathlib
import re
import sys

AURYN_PIN = re.compile(r'"(auryn-[a-z0-9-]+(?:\[[^\]"]*\])?)==[^"]*"')


def stamp(version: str, root: pathlib.Path) -> list[pathlib.Path]:
    files = [root / "pyproject.toml", *sorted((root / "packages").glob("*/pyproject.toml"))]
    for f in files:
        text, pins = AURYN_PIN.subn(lambda m: f'"{m.group(1)}=={version}"', f.read_text())
        f.write_text(text)
        print(f"{f} -> {version} ({pins} pins)")
    print(f"stamped {len(files)} packages")
    return files


if __name__ == "__main__":
    stamp(sys.argv[1], pathlib.Path(sys.argv[2] if len(sys.argv) > 2 else "."))
