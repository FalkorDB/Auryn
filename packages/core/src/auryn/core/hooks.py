"""Install / remove Auryn's git resync hooks in a repo (ADR 0003, `commit` trigger).

When a repo's trigger is `commit`, we drop a small block into its post-commit / post-checkout /
post-merge / post-rewrite hooks that fires an incremental resync in the background. Chained
politely: the block is fenced by markers, placed right after the shebang of (never overwriting)
any existing hook -- ahead of the user's own body, so an `exit 0` / `exec` there can't skip it --
and removed cleanly on forget/uninstall. The hooks directory is resolved via git itself, so
linked worktrees are honored.

What we leave alone, rather than risk breaking a hook that isn't ours:
  * a hooks directory outside the repo's own git dir (`core.hooksPath`, or a `.git/hooks` that
    is a symlink): it is either tracked (`.husky/`, `.githooks/` -- our block, with this
    machine's interpreter path, would be one `git add -A` away from the whole team) or shared by
    every repo on the machine (each commit anywhere would resync the last repo ingested);
  * a hook that isn't a shell script (a `#!/usr/bin/env python3` hook can't run a shell line);
  * a hook that is a symlink (a write would go through it, into a tracked script or a hook
    manager's own file);
  * an existing hook's file mode (a hook the user disabled stays disabled) and line endings.
Earlier versions wrote into all of these, so skipping one also takes an old block back out of it.
"""

from __future__ import annotations

import contextlib
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
    # Backgrounded from a subshell: the job is then no child of the hook's own shell, so a `wait`
    # in the user's body -- which the block sits ahead of -- returns without it.
    line = " ".join(shlex.quote(c) for c in sync_cmd)
    return f"{_BEGIN}\n({line} >/dev/null 2>&1 &)\n{_END}\n"


def _strip_block(text: str) -> str:
    """`text` without our block. A begin marker whose end marker is gone (deleted by hand) bounds
    nothing, so what follows it is kept: the block sits ahead of the user's body, and reading on
    to the end of the file would take the user's own lines out with it."""
    out, block = [], None  # block: the lines since a begin marker whose end marker is still to come
    for ln in text.splitlines(keepends=True):
        s = ln.strip()
        if s == _BEGIN:
            out.extend(block or [])  # one already open had no end marker: it bounds nothing
            block = [ln]
        elif s == _END and block is not None:
            block = None
        elif block is not None:
            block.append(ln)
        else:
            out.append(ln)
    return "".join(out + (block or []))


def _own_hooks_dir(local_dir) -> Path:
    """`<git common dir>/hooks`: where git looks for hooks unless `core.hooksPath` says otherwise."""
    out = subprocess.run(
        ["git", "-C", str(local_dir), "rev-parse", "--git-common-dir"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    common = Path(out) if Path(out).is_absolute() else Path(local_dir) / out
    return common.resolve() / "hooks"


def owns_hooks_dir(local_dir) -> bool:
    """True when the hooks directory is the repo's own `<git dir>/hooks`, i.e. neither
    `core.hooksPath` nor a symlink in its place redirects it into the work tree or to a directory
    other repos share (see the module docstring)."""
    return hooks_dir(local_dir).resolve() == _own_hooks_dir(local_dir)  # not through a `hooks` symlink


def _read(path: Path) -> str | None:
    """A hook's text, line endings untranslated (a CRLF hook is written back CRLF), or None for
    one that isn't text at all (a compiled hook) or that we may not read -- never ours to edit."""
    try:
        with path.open(newline="") as f:
            text = f.read()
    except (UnicodeDecodeError, OSError):
        return None
    return None if "\0" in text else text  # a compiled hook can decode all the same; text has no NUL


def _alias_of(path: Path, d: Path) -> str | None:
    """The hook in `d` that `path` is a symlink to (`post-merge -> post-commit`, one script for
    both events), else None. Such a link has no text of its own: whatever is read or written
    through it is the other hook's, which gets its block -- and loses it -- under its own name."""
    if not path.is_symlink():
        return None
    try:
        target = path.resolve()
    except (OSError, RuntimeError):  # a link that loops back on itself: not a hook of ours
        return None
    return target.name if target.parent == d.resolve() and target.name in HOOKS else None


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
    and short of HOOKS when an existing hook isn't a shell script, is a symlink or is disabled
    (see the module docstring)."""
    if not owns_hooks_dir(local_dir):
        with contextlib.suppress(OSError, ValueError):  # best effort: the directory may not be ours to write
            uninstall(local_dir)  # a block an earlier version put there
        return []
    d = hooks_dir(local_dir)
    d.mkdir(parents=True, exist_ok=True)
    block = _block(sync_cmd)
    wired = []
    aliases = {}  # a hook that is a link to another of ours -> that hook's name
    for name in HOOKS:
        path = d / name
        alias = _alias_of(path, d)
        if alias:
            aliases[name] = alias  # nothing to add or repair here: it runs the other hook's block
            continue
        if path.exists():
            text = _read(path)
            if text is None:
                continue
            base = _strip_block(text)
            if path.is_symlink() or not _is_shell(base):
                if base != text:  # a block an earlier version put there
                    path.write_text(base, newline="")
                continue
            if not base.strip():
                base = "#!/bin/sh\n"  # it held only our block
            # Right after the shebang -- ahead of the user's body, whose `exit` / `exec` would
            # otherwise end the hook before our (backgrounded) line. The file keeps its mode.
            head, _, body = base.partition("\n") if base.startswith("#!") else ("", "", base)
            eol = "\r\n" if "\r\n" in base else "\n"  # the block's lines end like the hook's own
            path.write_text((head + "\n" if head else "") + block.replace("\n", eol) + body, newline="")
        elif path.is_symlink():  # a dangling link: its target isn't ours to create
            continue
        else:
            path.write_text("#!/bin/sh\n" + block)
            path.chmod(0o755)
        if os.access(path, os.X_OK):  # git skips a non-executable hook: the block is in, but inert
            wired.append(name)
    return [name for name in HOOKS if name in wired or aliases.get(name) in wired]


def _swept_dirs(local_dir) -> list[Path]:
    """The hooks dir git uses now, and the repo's own one too when `core.hooksPath` has moved away
    from it since (husky sets it on `npm install`): a block left there runs again once the setting
    goes. Not a shared one."""
    current, own = hooks_dir(local_dir), _own_hooks_dir(local_dir)
    return [current] if own == current.resolve() or own.is_symlink() else [current, own]


def uninstall(local_dir) -> None:
    """Remove our block from every hook; delete a hook file that was only ours. A hook that
    doesn't carry our block is left byte-for-byte as it is. One we may not write -- or one whose
    begin marker lost its end marker, so the line under it may still run a sync -- doesn't end the
    sweep: the hooks after it are cleaned too, then its error is raised."""
    try:
        dirs = _swept_dirs(local_dir)
    except Exception:
        return
    failed = None
    for d in dirs:
        for name in HOOKS:
            path = d / name
            if not path.exists() or _alias_of(path, d):
                continue
            text = _read(path)
            if text is None or _BEGIN not in text:
                continue
            stripped = _strip_block(text)
            if _BEGIN in stripped:  # a begin marker without its end: the user's lines stay, and it is said
                failed = failed or ValueError(f"{path}: an auryn begin marker without its end marker -- "
                                              "remove it by hand")
            if stripped == text:  # no whole block to take out
                continue
            try:
                if stripped.strip() in _BARE and not path.is_symlink():  # a link: clean its target, keep it
                    path.unlink()
                else:
                    path.write_text(stripped, newline="")
            except OSError as e:
                failed = failed or e
    if failed:
        raise failed


def is_installed(local_dir) -> bool:
    try:
        dirs = _swept_dirs(local_dir)
    except Exception:
        return False
    return any((d / n).exists() and _BEGIN in (_read(d / n) or "") for d in dirs for n in HOOKS)
