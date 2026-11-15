"""Picklable dataclasses produced by file_parser (phase-1, per-file, no shared state -- safe
under a ProcessPoolExecutor). No tree-sitter node ever crosses this boundary; resolution.py
turns these into graph nodes/edges once every file has been parsed.

Everything here is what a single tree-sitter parse of one shell file can know on its own --
the functions it defines, the command *names* each function body invokes, and the `source`/`.`
includes anywhere in the file. Whether an invoked name resolves to a project function or is an
external command, and whether a `source`d path names a project file, are cross-file decisions
made later in resolution.py, where all files are visible at once.

Bash-specific shape (vs Go's raw_model):
- There are **no types, no classes, no packages** -- so there is no RawType/RawField/RawImport of
  types. The only declaration is `RawFunction`.
- A `RawCall` carries just a command *name* (no receiver): a bash call is `word args...`, so the
  callee is a single command word. Names that are not a static word (`$cmd`, `${x}sfx`) are
  dropped at parse time (unnameable, like Go's chained-selector calls).
- `RawSource` records a `source`/`.` include -- the Bash analog of an import.

line_start/line_end are 1-based inclusive (matching the other ingestors); 0 means not captured."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RawCall:
    name: str  # the invoked command word (a local function name or an external command)
    line_start: int = 0


@dataclass
class RawFunction:
    name: str
    calls: list[RawCall] = field(default_factory=list)
    source: str = ""
    doc: str = ""  # shared `doc` property: the leading `#` comment above the definition, cleaned
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawSource:
    """A `source X` / `. X` include. `path` is the argument text with surrounding quotes stripped;
    `is_dynamic` is True when the path contains a variable/command expansion (`$DIR/lib.sh`),
    which the fast pass cannot resolve statically and therefore skips."""

    path: str
    is_dynamic: bool = False
    line_start: int = 0


@dataclass
class RawFile:
    path: str
    functions: list[RawFunction] = field(default_factory=list)
    sources: list[RawSource] = field(default_factory=list)
