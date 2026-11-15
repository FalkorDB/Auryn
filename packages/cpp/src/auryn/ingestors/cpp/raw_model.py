"""Picklable dataclasses produced by file_parser (phase-1, per-file, no shared state -- safe
under a ProcessPoolExecutor). No tree-sitter node ever crosses this boundary; resolution.py
turns these into graph nodes/edges once every file has been parsed.

Everything here is what a single tree-sitter parse of one C++ translation unit can know on its
own -- names, types-as-written (text), storage/virtual/const flags, base clauses, template
parameters, call *names* (with receiver/scope). Cross-file decisions (does a base name a
project type? which class owns this out-of-line `Class::method` definition? does this call
resolve to a project method or free function?) are made later, in resolution.py, where all
files are visible at once.

Modelled after the union of the C ingestor (free functions/records/enums/macros/includes) and
the Java ingestor (types-with-members, hierarchy). line_start/line_end are 1-based inclusive
(from tree-sitter node positions), matching the C/Java/Python ingestors; 0 means not captured."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RawParameter:
    name: str  # "" for an unnamed prototype parameter
    type: str  # type as written, e.g. "int", "const Shape *", "Point"
    position: int


@dataclass
class RawCall:
    name: str  # the callee's simple name (function/method name, or the created type for `new`)
    receiver: str | None = None
    """The receiver's own simple name for `obj.m()` / `ptr->m()` / `this->m()` ("obj", "ptr",
    "this"), or None for a bare `foo()`, a `Class::m()` qualified call, or a receiver too
    complex to name. resolution.py uses it to prefer resolving against a field's/param's
    declared type."""
    scope: str | None = None  # the last scope segment of a qualified call `A::B::stat()` -> "B"
    is_new: bool = False  # `new Widget()` -> resolves against Widget's constructor


@dataclass
class RawMethod:
    name: str  # ctor == class name; dtor == "~Class"
    kind: str  # "ORDINARY" | "CONSTRUCTOR" | "DESTRUCTOR"
    return_type: str | None = None  # None for ctor/dtor
    params: list[RawParameter] = field(default_factory=list)
    is_static: bool = False
    is_virtual: bool = False
    is_const: bool = False
    is_definition: bool = False  # True = has a body (inline, or an out-of-line def)
    calls: list[RawCall] = field(default_factory=list)  # from the body (definitions only)
    # For an out-of-line `ReturnT Owner::method(...) {..}`: the owning type's simple name and the
    # full written scope chain (["A","B"] for A::B::method). Empty/None for an in-class member.
    owner_name: str | None = None
    owner_scope: list[str] = field(default_factory=list)
    namespace: str = ""  # the `::`-joined namespace the (out-of-line) definition sits in
    source: str = ""
    doc: str = ""  # shared `doc` property: the leading comment above the declaration, cleaned
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawField:
    name: str
    type: str  # type as written; keeps const/*/&/[]
    is_static: bool = False
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawBase:
    name: str  # base class simple name (generic args stripped)
    access: str  # "public" | "private" | "protected"


@dataclass
class RawType:
    kind: str  # "Class" | "Struct" | "Union" | "Enum"
    name: str
    namespace: str = ""  # "" for the global namespace
    is_scoped: bool = False  # Enum only: `enum class`/`enum struct` (scoped enumerator names)
    type_parameters: list[str] = field(default_factory=list)  # template type params
    bases: list[RawBase] = field(default_factory=list)  # class/struct only (EXTENDS)
    methods: list[RawMethod] = field(default_factory=list)
    fields: list[RawField] = field(default_factory=list)
    enum_members: list[str] = field(default_factory=list)
    nested_types: list["RawType"] = field(default_factory=list)
    is_definition: bool = True  # False = forward declaration (no body)
    source: str = ""
    doc: str = ""  # shared `doc` property: the leading comment above the declaration, cleaned
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawFunction:
    """A free (non-member) function. Member functions are RawMethod; out-of-line member
    *definitions* are RawMethod too (in RawFile.member_defs), so this stays purely free."""

    name: str
    namespace: str = ""
    return_type: str = ""
    params: list[RawParameter] = field(default_factory=list)
    is_definition: bool = False  # True = has a body; False = prototype
    calls: list[RawCall] = field(default_factory=list)
    signature: str = ""
    source: str = ""
    doc: str = ""  # shared `doc` property: the leading comment above the declaration, cleaned
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
    path: str  # as written, e.g. "geometry.hpp" or "vector"
    is_system: bool  # True for <...>, False for "..."


@dataclass
class RawFile:
    path: str
    includes: list[RawInclude] = field(default_factory=list)
    macros: list[RawMacro] = field(default_factory=list)
    types: list[RawType] = field(default_factory=list)  # top-level (namespaced or global) types
    functions: list[RawFunction] = field(default_factory=list)  # free functions
    member_defs: list[RawMethod] = field(default_factory=list)  # out-of-line `Class::method` defs
