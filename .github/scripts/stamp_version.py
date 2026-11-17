"""Stamp one version into every package of the workspace: the umbrella and packages/*.

Rewrites each pyproject's `[project] version` line AND every `auryn-*==<x>` pin (the umbrella's
dependencies, auryn-core's plugin extras), so a release stays lockstep: `falkordb-auryn==X`
installs exactly the X of every plugin.

Usage: python .github/scripts/stamp_version.py VERSION [ROOT]
Used by release.yml (the release version) and tests.yml (a local-only version for install tests).
"""

import pathlib
import re
import sys

VERSION_LINE = re.compile(r'(?m)^version = "[^"]*"')
AURYN_PIN = re.compile(r'"(auryn-[a-z-]+(?:\[[^\]"]*\])?)==[^"]*"')


def stamp(version: str, root: pathlib.Path) -> list[pathlib.Path]:
    files = [root / "pyproject.toml", *sorted((root / "packages").glob("*/pyproject.toml"))]
    for f in files:
        text, n = VERSION_LINE.subn(f'version = "{version}"', f.read_text(), count=1)
        if n != 1:
            raise SystemExit(f"no [project] version line found in {f}")
        text, pins = AURYN_PIN.subn(lambda m: f'"{m.group(1)}=={version}"', text)
        f.write_text(text)
        print(f"{f} -> {version} ({pins} pins)")
    print(f"stamped {len(files)} packages")
    return files


if __name__ == "__main__":
    stamp(sys.argv[1], pathlib.Path(sys.argv[2] if len(sys.argv) > 2 else "."))
