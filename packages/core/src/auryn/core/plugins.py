"""On-demand ingestor plugins: the core's catalog of official `auryn-<lang>` packages,
and the installer that pulls one from PyPI when a repo contains a file type no installed
plugin claims.

Together with the entry-point discovery in registry.py this closes the "lazy" loop: the
core ships knowing only *which package provides which language* (this catalog, no heavy
deps), detects the languages actually present in a repo, installs just those, and
re-discovers them.

Auto-install is OPT-OUT: on by default (so a repo "just works" for any catalogued
language), disabled per call (`auto_install=False`) or via `AURYN_AUTO_INSTALL=0`.

SECURITY / REPRODUCIBILITY: installing a plugin also (on the next discovery) IMPORTS and
runs third-party code, and mutates the environment non-deterministically. The guardrails
that make a default-on policy defensible: only the vetted **catalog** packages below are
ever installed (never an arbitrary/attacker-supplied name), every install is logged and
surfaced in the ingest report, and it's one flag to turn off. Production should still pin
plugin versions rather than float.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import logging
import os
import shutil
import subprocess
import sys

logger = logging.getLogger("auryn.plugins")

# suffix -> the PyPI package that provides an ingestor for it. The core's only knowledge of
# languages it doesn't itself carry; extend this as `auryn-<lang>` packages are published.
PLUGIN_PACKAGES: dict[str, str] = {
    ".c": "auryn-c",
    ".h": "auryn-c",
    ".cpp": "auryn-cpp",
    ".cc": "auryn-cpp",
    ".cxx": "auryn-cpp",
    ".c++": "auryn-cpp",
    ".hpp": "auryn-cpp",
    ".hh": "auryn-cpp",
    ".hxx": "auryn-cpp",
    ".h++": "auryn-cpp",
    ".cu": "auryn-cuda",
    ".cuh": "auryn-cuda",
    ".cs": "auryn-csharp",
    ".go": "auryn-go",
    ".rs": "auryn-rust",
    ".java": "auryn-java",
    ".jl": "auryn-julia",
    ".rb": "auryn-ruby",
    ".php": "auryn-php",
    ".ts": "auryn-typescript",
    ".tsx": "auryn-typescript",
    ".js": "auryn-typescript",    # the TypeScript plugin owns the whole JS/TS family (no auryn-javascript)
    ".jsx": "auryn-typescript",
    ".mjs": "auryn-typescript",
    ".cjs": "auryn-typescript",
    ".py": "auryn-python",
    ".kt": "auryn-kotlin",
    ".kts": "auryn-kotlin",
    ".swift": "auryn-swift",
    ".sh": "auryn-bash",
    ".bash": "auryn-bash",
}


def auto_install_enabled(explicit: bool | None) -> bool:
    """Resolve the opt-OUT policy: default ON. An explicit True/False wins; otherwise
    AURYN_AUTO_INSTALL can turn it off (0/false/no/off); anything else leaves it on."""
    if explicit is not None:
        return explicit
    return os.environ.get("AURYN_AUTO_INSTALL", "").strip().lower() not in ("0", "false", "no", "off")


def packages_for_suffixes(suffixes: set[str]) -> dict[str, str]:
    """Which catalog packages provide the given (unclaimed) suffixes -> {package: reason}.
    Deduplicated by package (`.c` and `.h` both map to auryn-c)."""
    out: dict[str, str] = {}
    for suffix in suffixes:
        pkg = PLUGIN_PACKAGES.get(suffix)
        if pkg:
            out.setdefault(pkg, suffix)
    return out


def _install_command(package: str) -> list[str]:
    """Prefer the project's toolchain (`uv pip install`) when available, else pip. Both forms target
    the RUNNING interpreter explicitly (`--python sys.executable` / `sys.executable -m pip`) so a
    runtime auto-install lands in the environment auryn is actually running from -- including an
    isolated `uv tool install` venv, where a bare `uv pip install` would target the current project's
    venv (or none) instead, leaving the package unimportable this process. `uv` refuses to touch a
    non-venv (system/global) interpreter without `--system`, so add it when sys.executable isn't in a
    venv."""
    if shutil.which("uv"):
        cmd = ["uv", "pip", "install", "--python", sys.executable]
        if sys.prefix == sys.base_prefix:  # system/global interpreter, not a virtualenv
            cmd.append("--system")
        return [*cmd, package]
    return [sys.executable, "-m", "pip", "install", package]


def install_plugin(package: str, label: str = "plugin") -> bool:
    """Install `package` into the running environment and invalidate import caches so it (an
    ingestor plugin, or an optional runtime dependency) is importable this process. Returns
    True on success. See the module docstring for the security posture -- callers must have
    opted in before calling this, and `package` must be a vetted/hardcoded name, never
    user-supplied. A catalog plugin is pinned to the running core's version: every release ships
    all plugins at that version, and the newest plugin's own pins (auryn-cuda -> auryn-cpp) could
    otherwise upgrade a plugin this process has already imported."""
    if package in PLUGIN_PACKAGES.values():
        package = f"{package}=={importlib.metadata.version('auryn-core')}"
    cmd = _install_command(package)
    logger.warning("Auto-installing %s %r via: %s", label, package, " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error("Could not install %r: %s", package, (result.stderr or result.stdout).strip()[:500])
        return False
    importlib.invalidate_caches()
    logger.info("Installed %s %r", label, package)
    return True


def ensure_importable(module: str, pip_spec: str | None = None, *, auto_install: bool | None = None):
    """Import and return `module`, auto-installing `pip_spec` (default: `module`) on demand if
    it's missing -- same opt-OUT policy as plugin install (on by default; off via
    AURYN_AUTO_INSTALL=0 or auto_install=False). Raises a clear, actionable ImportError if
    the module can't be made importable.

    This is what lets heavy, optional runtime *drivers* (oracledb, sqlalchemy) live behind a
    packaging extra without the file-based ingestors ever carrying them: a lean install omits
    them, and the first live run pulls just the one needed -- no manual pip step. The pip spec
    is always hardcoded by the caller (never derived from untrusted input), so the security
    posture matches install_plugin (see module docstring)."""
    try:
        return importlib.import_module(module)
    except ImportError:
        pass

    spec = pip_spec or module
    if not auto_install_enabled(auto_install):
        raise ImportError(
            f"'{module}' is required for this ingestor but is not installed, and auto-install "
            f"is disabled (AURYN_AUTO_INSTALL=0). Install it with:  pip install '{spec}'  "
            f"(or the packaged extra, e.g.  pip install 'auryn-oracle[live]')."
        )
    if not install_plugin(spec, label="dependency"):
        raise ImportError(
            f"Auto-install of '{spec}' failed (see the log above for pip's error). Install it "
            f"manually with:  pip install '{spec}'."
        )
    return importlib.import_module(module)
