"""Hatch metadata hook: pin every `auryn-*==` requirement to the version being built.

A package's own version comes from git (hatch-vcs), so the version its sibling packages must have
is only known when it is built. The requirements that carry such a pin are therefore declared
dynamic, and listed under `[tool.hatch.metadata.hooks.custom]` instead of `[project]`: the `==`
says WHICH of them move in lockstep, and this fills in the version. Every build from one tree then
fits the others from that tree -- a release, a wheel built from a clone, the editable installs of
`uv sync` -- and an sdist carries the result in its PKG-INFO.

The same file sits next to each pyproject.toml that has such requirements
(tests/test_package_extras.py keeps the copies identical): a hook has to be inside its own
package's sdist.
"""

import re

from hatchling.metadata.plugin.interface import MetadataHookInterface

AURYN_PIN = re.compile(r"^(auryn-[a-z0-9-]+(?:\[[^\]]*\])?)==.*$")


class PinToThisBuild(MetadataHookInterface):
    def update(self, metadata: dict) -> None:
        # The public version only: `==0.9.1.dev3` also matches `0.9.1.dev3+g<sha>.d<timestamp>`,
        # and each package built from a modified tree gets a timestamp of its own.
        version = metadata["version"].partition("+")[0]

        def pin(requirement: str) -> str:
            return AURYN_PIN.sub(rf"\1=={version}", requirement)

        if "dependencies" in self.config:
            metadata["dependencies"] = [pin(r) for r in self.config["dependencies"]]
        if "optional-dependencies" in self.config:
            metadata["optional-dependencies"] = {
                extra: [pin(r) for r in requirements]
                for extra, requirements in self.config["optional-dependencies"].items()
            }
