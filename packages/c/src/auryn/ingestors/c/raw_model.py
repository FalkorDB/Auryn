"""Picklable dataclasses produced by file_parser (phase-1, per-file, no shared state -- safe
under a ProcessPoolExecutor). No tree-sitter node ever crosses this boundary; resolution.py
turns these into graph nodes/edges once every file has been parsed.

Everything here is what a single tree-sitter parse of one .c/.h can know on its own -- names,
types-as-written (text), storage class, call *names*. Cross-file/linkage decisions
(is this call the extern or a static? does this include resolve to a project file?) are made
later, in resolution.py, where all files are visible at once."""

from __future__ import annotations

from dataclasses import dataclass, field


# line_start/line_end are 1-based inclusive line ranges (from tree-sitter node positions),
# mirroring the Python ingestor's line_start/line_end so consumers see the same location
# properties across languages. 0 means "not captured".


@dataclass
class RawParameter:
    name: str  # "" for an unnamed prototype parameter
    type: str  # type as written, e.g. "int", "char *", "struct Point"
    position: int


@dataclass
class RawFunction:
    name: str
    return_type: str
    params: list[RawParameter] = field(default_factory=list)
    is_static: bool = False
    is_definition: bool = False  # True = has a body (function_definition); False = prototype
    calls: list[str] = field(default_factory=list)  # callee names seen in the body (definitions only)
    signature: str = ""  # header exactly as written: return type + name + precise param list, no body
    source: str = ""  # the entity's source text (as C#/PL/SQL store it) -- get_context returns it
    doc: str = ""  # shared `doc` property: the leading comment above the declaration, cleaned
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawField:
    name: str
    type: str  # type as written; type_ref is the bare tag/typedef name if it names one
    type_ref: str | None = None
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawRecord:
    kind: str  # "struct" | "union"
    name: str
    fields: list[RawField] = field(default_factory=list)
    is_definition: bool = False  # True = has a body; False = forward declaration
    source: str = ""
    doc: str = ""  # shared `doc` property: the leading comment above the declaration, cleaned
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawEnum:
    name: str
    members: list[str] = field(default_factory=list)
    is_definition: bool = False
    source: str = ""
    doc: str = ""  # shared `doc` property: the leading comment above the declaration, cleaned
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawTypedef:
    name: str
    underlying: str  # aliased type as written
    underlying_ref: str | None = None  # bare tag/typedef name the alias points at, if any
    source: str = ""
    doc: str = ""  # shared `doc` property: the leading comment above the declaration, cleaned
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawGlobal:
    name: str
    type: str
    is_static: bool = False
    source: str = ""
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawMacro:
    name: str
    is_function_like: bool = False
    source: str = ""
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawInclude:
    path: str  # as written, e.g. "widget.h" or "stdio.h"
    is_system: bool  # True for <...>, False for "..."


@dataclass
class RawFile:
    path: str
    includes: list[RawInclude] = field(default_factory=list)
    functions: list[RawFunction] = field(default_factory=list)
    records: list[RawRecord] = field(default_factory=list)
    enums: list[RawEnum] = field(default_factory=list)
    typedefs: list[RawTypedef] = field(default_factory=list)
    globals: list[RawGlobal] = field(default_factory=list)
    macros: list[RawMacro] = field(default_factory=list)
