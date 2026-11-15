"""Picklable dataclasses produced by file_parser (phase-1, per-file, no shared state -- safe
under a ProcessPoolExecutor). No tree-sitter node ever crosses this boundary; resolution.py
turns these into graph nodes/edges once every file has been parsed.

Everything here is what a single tree-sitter parse of one .jl file can know on its own --
names, types-as-written (the `::T` annotation text), the module nesting, call *names*. Cross-
file/cross-scope decisions (does a supertype name a project type? does a call resolve to a
project generic function? which types does a method dispatch on?) are made later, in
resolution.py, where all files are visible at once.

Julia's model, faithfully: there are NO classes. A `RawModule` is a scope (`module X ... end`);
a file's top-level items live directly on the RawFile (Julia files are not modules). `RawType`
covers `struct`/`mutable struct` (Struct), `abstract type` (AbstractType) and `primitive type`
(PrimitiveType). A `RawMethod` is ONE concrete definition (`function f(x::Int)` or the short
form `f(x) = ...`); resolution groups the RawMethods that share a (scope, name) under a single
generic Function. line_start/line_end are 1-based inclusive; 0 means not captured."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RawParameter:
    name: str  # "" for an anonymous dispatch parameter, e.g. `::Int`
    type: str  # the `::T` annotation text as written ("" if untyped), e.g. "Circle", "Vector{String}", "T"
    position: int
    is_vararg: bool = False  # `rest...` / `rest::T...`
    is_keyword: bool = False  # a keyword argument (after the `;` in the signature)


@dataclass
class RawCall:
    name: str  # the callee's simple name (`foo` for `foo(...)`, `show` for `Base.show(...)`)
    receiver: str | None = None
    """The receiver's own simple name when the callee is `recv.name(...)` and `recv` is a bare
    identifier ("obj" for `obj.f()`, "Base" for `Base.show()`); None for a bare `foo()`."""


@dataclass
class RawMethod:
    name: str
    parameters: list[RawParameter] = field(default_factory=list)
    return_type: str | None = None  # `::T` on the signature, or None
    type_parameters: list[str] = field(default_factory=list)  # `where {T,S}` / parametric type vars
    is_short: bool = False  # short form `f(x) = ...` vs long form `function f(x) ... end`
    calls: list[RawCall] = field(default_factory=list)
    source: str = ""
    doc: str = ""  # leading doc: a `"""..."""` docstring above the def, else a `#` comment block
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawField:
    name: str
    type: str = ""  # the `::T` annotation text ("" if untyped)
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawType:
    kind: str  # "Struct" | "AbstractType" | "PrimitiveType"
    name: str
    type_parameters: list[str] = field(default_factory=list)  # `Foo{T,U}` -> ["T","U"]
    supertype: str | None = None  # from `<:` (simple name), e.g. `Shape` for `struct C <: Shape`
    is_mutable: bool = False  # `mutable struct` (Struct only)
    bits: int | None = None  # `primitive type X 8 end` -> 8 (PrimitiveType only)
    fields: list[RawField] = field(default_factory=list)  # Struct only
    source: str = ""
    doc: str = ""  # leading doc: a `"""..."""` docstring above the decl, else a `#` comment block
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawUsing:
    module: str  # the imported module's simple name (`LinearAlgebra`, `Base`)
    names: list[str] = field(default_factory=list)  # selected imports (`import Base: show, length`)
    is_import: bool = False  # `import` (vs `using`)


@dataclass
class RawModule:
    name: str  # simple name of this `module X` block
    types: list[RawType] = field(default_factory=list)
    functions: list[RawMethod] = field(default_factory=list)
    submodules: list["RawModule"] = field(default_factory=list)
    source: str = ""
    doc: str = ""  # leading doc: a `"""..."""` docstring above `module X`, else a `#` comment block
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawFile:
    """The top scope (project-level): items here are declared outside any `module` block and so
    hang off the Project. `modules` are the file's top-level `module X` blocks."""

    path: str
    usings: list[RawUsing] = field(default_factory=list)
    types: list[RawType] = field(default_factory=list)
    functions: list[RawMethod] = field(default_factory=list)
    modules: list[RawModule] = field(default_factory=list)
