"""Picklable dataclasses produced by file_parser (phase-1, per-file, no shared state -- safe
under a ProcessPoolExecutor). No tree-sitter node ever crosses this boundary; resolution.py
turns these into graph nodes/edges once every file has been parsed.

Everything here is what a single tree-sitter parse of one .swift file can know on its own --
names, types-as-written (text), access levels, argument labels, call *names* + shapes. Cross-file
decisions (does a call resolve to a project method? does a conformed name name a project protocol?
which type does a receiver have? which project type does an `extension` attach to?) are made
later, in resolution.py, where all files are visible at once.

Swift is a hybrid of Rust and Java (see schema.py). A type (class/struct/enum/protocol) is a
single `RawType` discriminated by `kind`; a callable is a single `RawFunction` whether free or
bound -- resolution labels it Function (from a file's top-level `functions`) or Method (from a
RawType's own `methods`, or a RawExtension's `methods`). Like Rust's `impl` blocks, an
`extension` (RawExtension) carries members that belong to a type declared elsewhere, plus any
protocol conformances it adds. line_start/line_end are 1-based inclusive; 0 means not captured."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RawParameter:
    name: str  # the internal parameter name (the binding used in the body)
    type: str  # type as written, e.g. "String", "[Int]", "() -> Void"
    position: int
    label: str = ""  # the argument label a caller writes ("to"); "" when suppressed (`_ name`)
    is_variadic: bool = False  # `Int...`


@dataclass
class RawCall:
    name: str  # the callee's simple name (final navigation segment, or the bare/constructed name)
    receiver: str | None = None
    """The receiver key resolution.py resolves against: "self", a bare variable/param/property
    name ("store" for `store.m()`), a type name for a static call ("Store" for `Store.make()`),
    or the last segment of a chain ("store" for `self.store.m()`). None for a bare call `foo()` /
    a construction `Foo(...)` (resolved by name against functions then types), or a receiver too
    complex to name (a call chain, a subscript, ...)."""


@dataclass
class RawFunction:
    name: str  # the func name, or "init" for an initializer
    kind: str  # "FUNCTION" (free) | "METHOD" (member) | "INITIALIZER" -- resolution uses the source list
    accessibility: str  # "open" | "public" | "internal" | "fileprivate" | "private"
    modifiers: list[str] = field(default_factory=list)  # static/class/final/override/mutating/async/...
    attributes: list[str] = field(default_factory=list)  # @objc, @discardableResult, ... (names only)
    return_type: str | None = None  # None for an initializer / a `-> Void` elided return
    parameters: list[RawParameter] = field(default_factory=list)
    calls: list[RawCall] = field(default_factory=list)
    is_static: bool = False  # `static`/`class` member -- a type-level function (no instance self)
    has_body: bool = True  # False for a protocol requirement (no `{ ... }`)
    source: str = ""
    doc: str = ""  # shared `doc` property: the leading `///`/`//`/`/** */` comment, cleaned
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawProperty:
    name: str
    type: str  # declared type as written; "" when only an initializer gives it (not inferred here)
    accessibility: str = "internal"
    modifiers: list[str] = field(default_factory=list)
    is_static: bool = False
    is_computed: bool = False  # has a `{ get ... }` / `{ ... }` body rather than storage
    is_constant: bool = False  # `let` vs `var`
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawEnumMember:
    name: str
    associated_values: list[str] = field(default_factory=list)  # e.g. ["Int", "String"] for `case x(Int, String)`
    raw_value: str | None = None  # e.g. '"R"' for `case raw = "R"`


@dataclass
class RawType:
    kind: str  # "Class" | "Struct" | "Enum" | "Interface"
    name: str
    accessibility: str = "internal"
    modifiers: list[str] = field(default_factory=list)
    attributes: list[str] = field(default_factory=list)
    type_parameters: list[str] = field(default_factory=list)
    inherits: list[str] = field(default_factory=list)
    """The comma-separated inheritance clause names, in source order (`class Dog: Animal, Named`
    -> ["Animal", "Named"]). resolution.py splits these into EXTENDS (a resolved project Class,
    or -- for a protocol -- a resolved project Interface) vs IMPLEMENTS (a resolved project
    Interface on a class/struct/enum), by the resolved target's kind; an enum's raw-value type
    (`enum E: String`) also lands here and simply resolves to nothing."""
    methods: list[RawFunction] = field(default_factory=list)  # incl. initializers (kind == "INITIALIZER")
    properties: list[RawProperty] = field(default_factory=list)
    enum_members: list[RawEnumMember] = field(default_factory=list)  # Enum only
    nested_types: list["RawType"] = field(default_factory=list)
    source: str = ""
    doc: str = ""  # shared `doc` property: the leading `///`/`//`/`/** */` comment, cleaned
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawExtension:
    """An `extension Foo: Bar { ... }` block. Like a Rust `impl`, its members belong to the
    extended type `type_name` (which may be declared in another file), and it can add protocol
    conformances. resolution.py attaches the members by resolving `type_name` to a project type."""

    type_name: str  # the extended type's simple name (`Point` for `extension Point`)
    conformances: list[str] = field(default_factory=list)  # protocols this extension adds
    methods: list[RawFunction] = field(default_factory=list)  # incl. initializers
    properties: list[RawProperty] = field(default_factory=list)
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawImport:
    name: str  # the imported module's name, e.g. "Foundation" (a `import struct A.B` keeps "A")


@dataclass
class RawFile:
    """A parsed .swift file. Its top-level `types`/`functions` hang off the project root (Swift has
    no per-file module declaration); `extensions` are attached to their target type in resolution;
    `imports` name external modules."""

    path: str
    imports: list[RawImport] = field(default_factory=list)
    types: list[RawType] = field(default_factory=list)
    functions: list[RawFunction] = field(default_factory=list)  # top-level free functions
    extensions: list[RawExtension] = field(default_factory=list)
