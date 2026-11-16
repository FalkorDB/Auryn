"""Install / remove Auryn's git resync hooks in a repo (ADR 0003, `commit` trigger).

When a repo's trigger is `commit`, we drop a small block into its post-commit / post-checkout /
post-merge / post-rewrite hooks that fires an incremental resync in the background. Chained
politely: the block is fenced by markers, placed right after the shebang of (never overwriting)
any existing hook -- ahead of the user's own body, so an `exit 0` / `exec` there can't skip it --
and removed cleanly on forget/uninstall. The hooks directory is resolved via git itself, so
linked worktrees are honored.

What we leave alone, rather than risk breaking a hook that isn't ours:
  * a hooks directory outside the repo's own git dir (`core.hooksPath`): it is either tracked
    (`.husky/`, `.githooks/` -- our block, with this machine's interpreter path, would be one
    `git add -A` away from the whole team) or shared by every repo on the machine (each commit
    anywhere would resync the last repo ingested);
  * a hook that isn't a shell script (a `#!/usr/bin/env python3` hook can't run a shell line);
  * an existing hook's file mode (a hook the user disabled stays disabled).
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

HOOKS = ("post-commit", "post-checkout", "post-merge", "post-rewrite")
_BEGIN = "# >>> auryn resync >>>"
_END = "# <<< auryn resync <<<"
_BARE = ("", "#!/bin/sh", "#!/bin/bash", "#!/usr/bin/env bash")
_SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh", "ash"})


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


def _owns_hooks_dir(local_dir, d: Path) -> bool:
    """True when `d` is the repo's own `<git dir>/hooks`, i.e. `core.hooksPath` doesn't redirect
    it into the work tree or to a directory other repos share (see the module docstring)."""
    out = subprocess.run(
        ["git", "-C", str(local_dir), "rev-parse", "--git-common-dir"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    common = Path(out) if Path(out).is_absolute() else Path(local_dir) / out
    return d.resolve() == (common / "hooks").resolve()


def _read(path: Path) -> str | None:
    """A hook's text, or None for one that isn't text at all (a compiled hook) -- never ours to edit."""
    try:
        return path.read_text()
    except UnicodeDecodeError:
        return None


def _is_shell(text: str) -> bool:
    """Whether a hook body can take a shell line: no shebang (git runs it with sh) or a shell one."""
    first = text.split("\n", 1)[0]
    if not first.startswith("#!"):
        return True
    words = [w for w in first[2:].split() if not w.startswith("-")]
    if words and Path(words[0]).name == "env":
        words = words[1:]
    return bool(words) and Path(words[0]).name in _SHELLS


def install(local_dir, sync_cmd: list[str]) -> list[str]:
    """Add (or refresh) our resync block in every hook, preserving any existing hook body.
    Returns the hooks that will now run it -- empty when the hooks directory isn't the repo's own,
    and short of HOOKS when an existing hook isn't a shell script or is disabled (see the module
    docstring)."""
    d = hooks_dir(local_dir)
    if not _owns_hooks_dir(local_dir, d):
        return []
    d.mkdir(parents=True, exist_ok=True)
    block = _block(sync_cmd)
    wired = []
    for name in HOOKS:
        path = d / name
        if path.exists():
            text = _read(path)
            if text is None or not _is_shell(_strip_block(text)):
                continue
            base = _strip_block(text)
            if not base.strip():
                base = "#!/bin/sh\n"  # it held only our block
            # Right after the shebang -- ahead of the user's body, whose `exit` / `exec` would
            # otherwise end the hook before our (backgrounded) line. The file keeps its mode.
            head, _, body = base.partition("\n") if base.startswith("#!") else ("", "", base)
            path.write_text((head + "\n" if head else "") + block + body)
        else:
            path.write_text("#!/bin/sh\n" + block)
            path.chmod(0o755)
        if os.access(path, os.X_OK):  # git skips a non-executable hook: the block is in, but inert
            wired.append(name)
    return wired


def uninstall(local_dir) -> None:
    """Remove our block from every hook; delete a hook file that was only ours. A hook that
    doesn't carry our block is left byte-for-byte as it is."""
    try:
        d = hooks_dir(local_dir)
    except Exception:
        return
    for name in HOOKS:
        path = d / name
        if not path.exists():
            continue
        text = _read(path)
        if text is None or _BEGIN not in text:
            continue
        stripped = _strip_block(text)
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
