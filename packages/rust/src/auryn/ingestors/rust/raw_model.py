"""Picklable dataclasses produced by file_parser (phase-1, per-file, no shared state -- safe
under a ProcessPoolExecutor). No tree-sitter node ever crosses this boundary; resolution.py
turns these into graph nodes/edges once every file has been parsed.

Everything here is what a single tree-sitter parse of one .rs file can know on its own -- names,
types-as-written (text), visibility, call *names* + shapes. Cross-file/module decisions (does a
call resolve to a project function? does an `impl Trait for Type` name a project trait? which
type does a receiver have?) are made later, in resolution.py, where all files are visible.

Rust is module-based: a file (crate-root scope) and every inline `mod` inside it are scopes that
carry the same item lists (types/impls/functions/consts/modules). A type (struct/enum/trait) is a
single `RawType` discriminated by `kind`; a callable is a single `RawFunction` whether free or
bound -- resolution labels it Function (from a scope's `functions`) or Method (from a RawImpl's
`methods` or a trait's `trait_methods`), with `is_associated`/`self_kind` distinguishing a method
from a no-`self` associated function. A type's inherent/trait methods are NOT stored on the type
-- they live in RawImpl blocks (an impl can sit in a different file than its struct) and are
attached by name in resolution. line_start/line_end are 1-based inclusive; 0 means not captured."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RawParameter:
    name: str
    type: str  # type as written, e.g. "&Store", "Vec<String>", "u32"
    position: int


@dataclass
class RawCall:
    name: str  # the callee's simple name (final path segment / method name)
    kind: str  # "free" (foo()) | "method" (x.m()) | "path" (Type::assoc() / mod::func())
    receiver: str | None = None
    """For a `method` call, the receiver key resolution.py resolves against: "self", a bare
    variable/param name ("store" for `store.m()`), or a field name ("store" for
    `self.store.m()`). None for a receiver too complex to name (a call chain, an index, ...)."""
    qualifier: str | None = None
    """For a `path` call, the immediate qualifier segment ("Store"/"Self" for `Store::new()`/
    `Self::new()`, "model" for `crate::model::helper()`) -- resolution tries it as a type first."""
    path: str | None = None
    """For a `path` call, the FULL qualifier path as written ("std::mem" for `std::mem::swap()`,
    "crate::model" for `crate::model::helper()`, "Store" for `Store::new()`). Lets resolution build
    a cross-repo qualified name and classify an unresolved call's origin (stdlib vs external) from
    the leading segment (ADR 0004) -- `qualifier` keeps only the immediate one. None otherwise."""


@dataclass
class RawFunction:
    name: str
    kind: str  # "FUNCTION" (free) | "METHOD" (impl/trait-block fn) -- resolution uses the source list
    visibility: str  # "pub" | "pub(crate)" | ... | "private"
    modifiers: list[str] = field(default_factory=list)  # async/unsafe/const/extern/default
    return_type: str | None = None
    parameters: list[RawParameter] = field(default_factory=list)  # excludes the self receiver
    calls: list[RawCall] = field(default_factory=list)
    unresolved_macro_calls: int = 0  # call-like sites inside macro invocations in the body -- the fast
    # engine can't resolve them (a macro body is a token tree, not parsed expressions), so they produce
    # no CALLS edge; counted so the function node can flag that its callees may be incomplete (#792)
    type_parameters: list[str] = field(default_factory=list)  # the fn's own generics (`fn f<T>()` -> T);
    # a method called on a `T`/`dyn`/`impl` receiver can't be statically dispatched to a concrete method
    is_associated: bool = False  # a method with no self receiver (e.g. `Store::new`)
    self_kind: str | None = None  # "value" (self) | "ref" (&self) | "mut_ref" (&mut self) | None
    has_body: bool = True  # False for a trait's required method (function_signature_item)
    source: str = ""
    doc: str = ""  # shared `doc` property: the leading `///`/`//`/`/** */` comment, cleaned
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawField:
    name: str  # a named field's name, or "0"/"1"/... for a tuple field
    type: str
    visibility: str = "private"
    position: int = 0
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawVariant:
    name: str
    kind: str  # "unit" | "tuple" | "struct"
    fields: list["RawField"] = field(default_factory=list)  # tuple/struct-variant fields; [] for unit
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawType:
    kind: str  # "Struct" | "Enum" | "Trait"
    name: str
    visibility: str = "private"
    type_parameters: list[str] = field(default_factory=list)
    supertraits: list[str] = field(default_factory=list)  # Trait only (`trait Foo: Bar + Baz`)
    fields: list[RawField] = field(default_factory=list)  # Struct only
    variants: list[RawVariant] = field(default_factory=list)  # Enum only
    trait_methods: list[RawFunction] = field(default_factory=list)  # Trait only (sig + default)
    source: str = ""
    doc: str = ""  # shared `doc` property: the leading `///`/`//`/`/** */` comment, cleaned
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawImpl:
    type_name: str  # the impl's `Self` type, bare simple name (`Store` for `impl Store`)
    trait_name: str | None = None  # implemented trait's simple name; None for an inherent impl
    type_parameters: list[str] = field(default_factory=list)  # the impl block's own generics (`impl<T>
    # Foo<T>` -> [T]); a method dispatching on one can't be statically resolved to a concrete method (#792)
    methods: list[RawFunction] = field(default_factory=list)
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawConst:
    name: str
    kind: str  # "const" | "static"
    type: str
    visibility: str = "private"
    source: str = ""
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawModule:
    name: str
    is_declaration: bool = False  # `mod x;` (True, external-file) vs inline `mod x { ... }` (False)
    visibility: str = "private"
    types: list[RawType] = field(default_factory=list)
    impls: list[RawImpl] = field(default_factory=list)
    functions: list[RawFunction] = field(default_factory=list)
    consts: list[RawConst] = field(default_factory=list)
    modules: list["RawModule"] = field(default_factory=list)
    source: str = ""
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawFile:
    """A parsed .rs file. Its own item lists are the crate-root (or file-module) scope; `modules`
    holds the inline `mod` items nested in it. Shares the item-list field names with RawModule so
    file_parser's collect_scope fills either interchangeably."""

    path: str
    uses: list[str] = field(default_factory=list)  # `use` paths as written (deep-pass fodder)
    types: list[RawType] = field(default_factory=list)
    impls: list[RawImpl] = field(default_factory=list)
    functions: list[RawFunction] = field(default_factory=list)
    consts: list[RawConst] = field(default_factory=list)
    modules: list[RawModule] = field(default_factory=list)
