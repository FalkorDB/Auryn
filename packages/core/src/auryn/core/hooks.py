"""Install / remove Auryn's git resync hooks in a repo (ADR 0003, `commit` trigger).

When a repo's trigger is `commit`, we drop a small block into its post-commit / post-checkout /
post-merge / post-rewrite hooks that fires an incremental resync in the background. Chained
politely: the block is fenced by markers, appended to (never overwriting) any existing hook,
and removed cleanly on forget/uninstall. The hooks directory is resolved via git itself, so
`core.hooksPath` and linked worktrees are honored.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

HOOKS = ("post-commit", "post-checkout", "post-merge", "post-rewrite")
_BEGIN = "# >>> auryn resync >>>"
_END = "# <<< auryn resync <<<"
_BARE = ("", "#!/bin/sh", "#!/bin/bash", "#!/usr/bin/env bash")


def is_git_repo(local_dir) -> bool:
    try:
        return subprocess.run(
            ["git", "-C", str(local_dir), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True,
        ).stdout.strip() == "true"
    except Exception:
        return False


def hooks_dir(local_dir) -> Path:
    """The repo's hooks directory, per git (honors core.hooksPath and worktrees)."""
    out = subprocess.run(
        ["git", "-C", str(local_dir), "rev-parse", "--git-path", "hooks"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    p = Path(out)
    return p if p.is_absolute() else Path(local_dir) / p


def _block(sync_cmd: list[str]) -> str:
    line = " ".join(shlex.quote(c) for c in sync_cmd)
    return f"{_BEGIN}\n{line} >/dev/null 2>&1 &\n{_END}\n"


def _strip_block(text: str) -> str:
    out, skip = [], False
    for ln in text.splitlines(keepends=True):
        s = ln.strip()
        if s == _BEGIN:
            skip = True
        elif s == _END:
            skip = False
        elif not skip:
            out.append(ln)
    return "".join(out)


def install(local_dir, sync_cmd: list[str]) -> None:
    """Add (or refresh) our resync block in every hook, preserving any existing hook body."""
    d = hooks_dir(local_dir)
    d.mkdir(parents=True, exist_ok=True)
    block = _block(sync_cmd)
    for name in HOOKS:
        path = d / name
        if path.exists():
            base = _strip_block(path.read_text()) or "#!/bin/sh\n"
            text = (base if base.endswith("\n") else base + "\n") + block
        else:
            text = "#!/bin/sh\n" + block
        path.write_text(text)
        path.chmod(0o755)


def uninstall(local_dir) -> None:
    """Remove our block from every hook; delete a hook file that was only ours."""
    try:
        d = hooks_dir(local_dir)
    except Exception:
        return
    for name in HOOKS:
        path = d / name
        if not path.exists():
            continue
        stripped = _strip_block(path.read_text())
        if stripped.strip() in _BARE:
            path.unlink()
        else:
            path.write_text(stripped)


def is_installed(local_dir) -> bool:
    try:
        d = hooks_dir(local_dir)
    except Exception:
        return False
    return any((d / n).exists() and _BEGIN in (d / n).read_text() for n in HOOKS)
