"""Check an Auryn install in CI. Run it with the Python of the environment under test.

Usage: python check_install.py VERSION

Fails if an installed Auryn package is not VERSION (the locally built wheels, not an older release
from PyPI), or if the CLI or a plugin does not load.
"""

import importlib
import importlib.metadata
import sys

version = sys.argv[1]
installed = {
    d.metadata["Name"].lower(): d.version
    for d in importlib.metadata.distributions()
    if d.metadata["Name"].lower().startswith(("auryn-", "falkordb-auryn"))
}
assert "auryn-core" in installed, f"auryn-core is not installed: {sorted(installed)}"
stale = {name: v for name, v in installed.items() if v != version}
assert not stale, f"not the locally built version {version}: {stale}"

importlib.import_module("auryn.cli")
plugins = [
    entry_point.load()
    for group in ("auryn.languages", "auryn.ingestors", "auryn.enrichments")
    for entry_point in importlib.metadata.entry_points(group=group)
]
print(
    f"{len(installed)} packages at {version}, {len(plugins)} plugins loaded: {sorted(installed)}"
)
