"""Go module (`go.mod`) awareness for the Go ingestor.

A repo can hold many packages, and two directories can declare the SAME `package` clause name
(`package main` in every `cmd/*`; common names like `util`/`handlers`/`models`). Keying a package
by its bare clause name collapses those onto one node and makes cross-repo healing ambiguous. The
fix keys a package by its DIRECTORY (unique per package -- Go has one package per dir) and makes
cross-repo linkage keys IMPORT-PATH-qualified, which requires the module path from `go.mod`.

Two layers, mirroring the Rust ingestor's cargo.py:

1. **Pure directory identity** -- the package id is the file's repo-relative directory (no manifest,
   no source access), so a full ingest and an incremental resync compute it identically (the delta
   callbacks get only a path). This is what fixes the collision.

2. **`go.mod` parsing + the module map** -- `module_path`, `go` version, and `require` deps. The
   module map (`{module_dir: module_path}`) turns a package's directory into its canonical import
   path (`module_path` + the dir relative to the owning module), which is what an external consumer
   writes in an `import` and therefore what both sides of a cross-repo heal must agree on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


def _norm(path: str) -> str:
    return path.replace("\\", "/").strip("/")


@dataclass
class GoRequire:
    module_path: str   # the required module path, e.g. "github.com/pkg/errors"
    version: str       # the version requirement, e.g. "v0.9.1" ("" if absent)
    indirect: bool     # a `// indirect` dependency


@dataclass
class GoModule:
    dir: str                 # the go.mod's directory (""/repo-root for a top-level module)
    module_path: str | None  # the `module` line path, e.g. "github.com/acme/widgets"
    go_version: str | None   # the `go` directive, e.g. "1.22"
    requires: list[GoRequire] = field(default_factory=list)


_MODULE_RE = re.compile(r"^\s*module\s+(\S+)", re.MULTILINE)
_GO_RE = re.compile(r"^\s*go\s+(\S+)", re.MULTILINE)
# A single-line require: `require github.com/x/y v1.2.3` (optionally `// indirect`).
_REQUIRE_LINE_RE = re.compile(r"^\s*require\s+(\S+)\s+(\S+)\s*(//\s*indirect)?\s*$", re.MULTILINE)
# A require block: `require (\n  github.com/x/y v1.2.3 // indirect\n  ...\n)`.
_REQUIRE_BLOCK_RE = re.compile(r"^\s*require\s*\(\s*$(.*?)^\s*\)\s*$", re.MULTILINE | re.DOTALL)
_BLOCK_ENTRY_RE = re.compile(r"^\s*(\S+)\s+(\S+)\s*(//\s*indirect)?\s*$")


def parse_gomod(dir_path: str, data: bytes) -> GoModule:
    """Parse one `go.mod` (best-effort -- never raises; a malformed file yields an empty module)."""
    text = data.decode("utf-8", "replace")
    m = GoModule(dir=_norm(dir_path), module_path=None, go_version=None)

    mod = _MODULE_RE.search(text)
    if mod:
        m.module_path = mod.group(1).strip('"')
    gov = _GO_RE.search(text)
    if gov:
        m.go_version = gov.group(1)

    for line_match in _REQUIRE_LINE_RE.finditer(text):
        path, ver, indirect = line_match.groups()
        m.requires.append(GoRequire(path.strip('"'), ver, bool(indirect)))
    for block in _REQUIRE_BLOCK_RE.finditer(text):
        for raw in block.group(1).splitlines():
            if not raw.strip() or raw.strip().startswith("//"):
                continue
            entry = _BLOCK_ENTRY_RE.match(raw)
            if entry:
                path, ver, indirect = entry.groups()
                m.requires.append(GoRequire(path.strip('"'), ver, bool(indirect)))
    return m


def build_module_map(modules: list[GoModule]) -> dict[str, str]:
    """`{module_dir: module_path}` for every go.mod that declares a module path -- the lookup that
    turns a package directory into its import path."""
    return {mod.dir: mod.module_path for mod in modules if mod.module_path}


def owning_module_dir(file_dir: str, module_dirs) -> str | None:
    """The directory of the go.mod that owns `file_dir` -- its NEAREST ancestor (incl. itself) that
    declares a module. None if no module covers it (a repo with no go.mod)."""
    d = _norm(file_dir)
    best: str | None = None
    for md in module_dirs:
        md_n = _norm(md)
        # md_n is an ancestor of d (or equal) when d == md_n or d starts with md_n + "/".
        if d == md_n or (md_n == "" ) or d.startswith(md_n + "/"):
            if best is None or len(md_n) > len(best):
                best = md_n
    return best


def import_path_for_dir(file_dir: str, module_map: dict[str, str]) -> str:
    """The canonical import path of the package in `file_dir`: the owning module's path + the dir
    relative to that module. Falls back to the repo-relative dir when no go.mod covers it (so a
    module-less repo still keys packages distinctly by directory, just without a module prefix)."""
    d = _norm(file_dir)
    owner = owning_module_dir(d, module_map.keys())
    if owner is None:
        return d  # no module: best-effort repo-relative dir
    module_path = module_map[owner]
    rel = d[len(owner):].lstrip("/") if owner else d
    return f"{module_path}/{rel}" if rel else module_path
