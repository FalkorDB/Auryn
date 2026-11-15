"""Which repo a command is operating on, and the graph name that repo maps to.

Default is one graph per repository/working directory. The repo root is taken from (in order)
an explicit path, then ``CLAUDE_PROJECT_DIR`` (the env var Claude Code sets on a spawned MCP
server -- the subprocess cwd is NOT guaranteed to be the project, so we rely on the env var),
then the current directory. The graph name is the directory's basename plus a short hash of its
absolute path, so two repos that share a basename don't collide into one graph. A caller can
always override with an explicit ``--graph-name`` (e.g. to ingest several repos into one graph).
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

_CLAUDE_PROJECT_DIR = "CLAUDE_PROJECT_DIR"


def resolve_project_root(path: str | None = None) -> Path:
    """The repo/working directory a command targets: explicit ``path`` if given, else
    ``CLAUDE_PROJECT_DIR`` (agent-spawned MCP server), else the cwd."""
    if path:
        return Path(path).expanduser().resolve()
    env = os.environ.get(_CLAUDE_PROJECT_DIR)
    if env:
        return Path(env).expanduser().resolve()
    return Path.cwd()


def _stable_name(base: str, seed: str) -> str:
    base = re.sub(r"[^A-Za-z0-9_-]", "_", base) or "graph"
    return f"{base}-{hashlib.sha256(seed.encode()).hexdigest()[:8]}"


def default_graph_name(root: Path | None = None) -> str:
    """A stable, collision-resistant graph name for ``root`` (default: the resolved project
    root): the sanitized basename plus 8 hex of a hash of the absolute path. Deterministic, so
    the same repo always maps to the same graph across sessions and reboots."""
    root = root or resolve_project_root()
    return _stable_name(root.name, str(root))


def graph_name_for_repo(uri: str) -> str:
    """A stable graph name for a git repo URI: its basename (minus ``.git``) plus a hash of the
    full URI, so two same-named repos from different hosts don't collide."""
    base = uri.rstrip("/").split("/")[-1]
    if base.endswith(".git"):
        base = base[:-4]
    return _stable_name(base, uri)
