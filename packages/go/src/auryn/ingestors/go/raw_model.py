"""Picklable dataclasses produced by file_parser (phase-1, per-file, no shared state -- safe
under a ProcessPoolExecutor). No tree-sitter node ever crosses this boundary; resolution.py
turns these into graph nodes/edges once every file has been parsed.

Everything here is what a single tree-sitter parse of one .go file can know on its own --
names, types-as-written (text), the *unqualified/qualified type-reference names* a signature or
field mentions (`type_refs`, computed from the AST so named return values and `pkg.Type`
qualifiers are handled precisely), and call *names* with their receiver identifier. Cross-file
decisions (does a bare name resolve to a project type in this package? does `m.Model` name a
project package's type? does `s.Put()` land on the receiver type's method?) are made later, in
resolution.py, where all files -- and thus the whole package set -- are visible at once.

Go-specific shape vs Java's raw_model:
- A **method is a top-level func with a receiver** (Go doesn't nest methods in the type body),
  so `RawFunction` carries an optional `RawReceiver`; resolution attaches it to its receiver
  type. Interface method *specs* live on the interface `RawType` (`methods`) instead.
- Structs carry `embeds` (anonymous/embedded fields) -> the Go-specific EMBEDS edge.
- `RawType.kind` is "Struct" | "Interface" | "TypeAlias" (named non-struct/non-interface types
  and `type X = Y` aliases both fold into TypeAlias).

line_start/line_end are 1-based inclusive (matching the Java/C/Python ingestors); 0 means not
captured."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RawParameter:
    name: str  # "" for an unnamed parameter (interface method specs, `func(int)`, results)
    type: str  # type as written, e.g. "*Store", "[]string", "map[string]int", "m.Model"
    position: int
    is_variadic: bool = False  # `nums ...int`


@dataclass
class RawCall:
    name: str  # the callee's simple name (bare func name, or the selector's field)
    receiver: str | None = None
    """The operand identifier when the call is `x.name()` and `x` is a bare identifier ("s" for
    `s.Put()`, "model" for `model.New()`); None for a bare `foo()`. resolution.py uses it to
    decide between a receiver/param/field-typed method call and a package-qualified function
    call. A receiver too complex to name (a chained selector like `a.b.c()`) yields no RawCall."""


@dataclass
class RawField:
    name: str
    type: str  # type as written
    type_refs: list[str] = field(default_factory=list)  # project-type refs, "Name" or "pkg.Name"
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawReceiver:
    name: str  # the receiver variable, e.g. "s" (Go's `this`); "" if omitted (`func (Store) F()`)
    type_name: str  # the receiver's owner type name, stripped of `*` and generic args ("Store")
    is_pointer: bool = False  # `func (s *Store)` vs `func (s Store)`


@dataclass
class RawFunction:
    name: str
    receiver: RawReceiver | None = None  # None -> free Function; set -> Method on receiver.type_name
    parameters: list[RawParameter] = field(default_factory=list)
    return_type: str = ""  # result as written ("", "int", "(int, error)", "*Store")
    type_refs: list[str] = field(default_factory=list)
    """Project-type references this callable makes: parameter types + result types + in-body
    construction sites (`Store{}` / `&Store{}` composite literals and `new(Store)`). Each is a
    "Name" (same-package) or "pkg.Name" (qualified) string; resolution turns the resolvable ones
    into DEPENDS_ON edges. Go has no constructor to CALL, so a construction site is modelled as a
    *type dependency*, the idiomatic analog of Java's `new X()` -> CALLS-a-constructor."""
    calls: list[RawCall] = field(default_factory=list)
    source: str = ""
    doc: str = ""  # shared `doc` property: the leading comment above the declaration, cleaned
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawTypeMethod:
    """A method *spec* declared inside an interface body (name + signature, no body/calls)."""

    name: str
    parameters: list[RawParameter] = field(default_factory=list)
    return_type: str = ""
    doc: str = ""
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawType:
    kind: str  # "Struct" | "Interface" | "TypeAlias"
    name: str
    type_parameters: list[str] = field(default_factory=list)
    fields: list[RawField] = field(default_factory=list)  # struct only
    embeds: list[str] = field(default_factory=list)
    """Embedded/anonymous members -> the EMBEDS edge: struct anonymous fields (`Base`, `*Base`,
    `pkg.Base`) and interface embedded interfaces (`Reader`, `io.Reader`). Each a "Name" or
    "pkg.Name" ref string; resolution keeps only those naming a project type."""
    methods: list[RawTypeMethod] = field(default_factory=list)  # interface method specs only
    underlying: str | None = None  # TypeAlias: the aliased/underlying type as written
    is_alias: bool = False  # TypeAlias: True for `type X = Y`, False for a defined named type
    source: str = ""
    doc: str = ""  # shared `doc` property: the leading comment above the declaration, cleaned
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawConst:
    name: str
    value: str = ""  # the RHS expression text, "" if elided (iota continuation)
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawImport:
    path: str  # the import path with quotes stripped, e.g. "github.com/acme/proj/model"
    alias: str | None = None  # explicit alias (`m "..."`), else None (defaults to path's last seg)


@dataclass
class RawFile:
    path: str
    package: str = ""  # the declared package name (`package store` -> "store")
    imports: list[RawImport] = field(default_factory=list)
    types: list[RawType] = field(default_factory=list)
    functions: list[RawFunction] = field(default_factory=list)  # free funcs AND methods
    consts: list[RawConst] = field(default_factory=list)
