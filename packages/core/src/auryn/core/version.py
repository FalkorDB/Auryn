"""`auryn --version`: which Auryn is installed, and the git commit it was built from.

Versions are derived from git at build time (hatch-vcs, see the pyproject files): a release tag
gives a plain version (`0.3.0`); any other commit gives `<next>.dev<N>+g<sha>`, and a build of a
modified tree appends `.d<UTC timestamp>`. So the version alone says which commit -- and whether a
modified tree -- an install came from. A release, or a build made without git, names no commit.
"""

from __future__ import annotations

import re
from importlib import metadata

_LOCAL = re.compile(r"\+g(?P<sha>[0-9a-f]{7,40})(?:\.d(?P<ts>\d{8}(?:\d{6})?))?")


def _dist_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def parse(version: str) -> dict:
    """{'commit': sha or None, 'modified': bool, 'built': 'YYYY-MM-DD HH:MM:SS UTC' or None}."""
    m = _LOCAL.search(version or "")
    if not m:
        return {"commit": None, "modified": False, "built": None}
    ts = m["ts"]
    built = None
    if ts:
        built = f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}" + (f" {ts[8:10]}:{ts[10:12]}:{ts[12:14]} UTC" if len(ts) == 14 else "")
    return {"commit": m["sha"], "modified": bool(ts), "built": built}


def describe() -> str:
    """The `auryn --version` line: the installed `auryn-core` version, plus the commit (and the
    build time of a modified tree) when the version carries them."""
    core = _dist_version("auryn-core") or "unknown"
    info = parse(core)
    if not info["commit"]:
        return f"auryn {core}"
    details = f"commit {info['commit']}"
    if info["modified"]:
        details += f"; built from a modified tree at {info['built']}"
    return f"auryn {core} ({details})"
