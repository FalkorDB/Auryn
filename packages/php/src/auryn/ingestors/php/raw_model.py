"""Picklable dataclasses produced by file_parser (phase-1, per-file, no shared state -- safe
under a ProcessPoolExecutor). No tree-sitter node ever crosses this boundary; resolution.py
turns these into graph nodes/edges once every file has been parsed.

Everything here is what a single tree-sitter parse of one .php file can know on its own -- names,
types-as-written (text), visibility/modifiers, call *names* + receivers. Cross-file decisions
(does this call resolve to a project method/function? does a base name name a project type?) are
made later, in resolution.py, where all files are visible at once.

Modelled after the Java ingestor's raw_model (PHP is a structural sibling: explicit classes/
interfaces with extends/implements), extended for PHP's own constructs: `Trait` types and the
`use TraitName;` uses list (USES_TRAIT), `Property` members with a visibility (PHP's term, matching
the C# precedent), class `Const`s, `EnumMember`s, top-level free `Function`s, and
constructor-promoted properties. A class has a single `superclass` plus a separate `interfaces`
list, so EXTENDS vs IMPLEMENTS needs no naming heuristic. line_start/line_end are 1-based
inclusive; 0 means not captured."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RawParameter:
    name: str  # without the leading `$`
    type: str  # type as written, e.g. "Store", "?Store", "int|string", ""
    position: int
    is_variadic: bool = False  # `Type ...$args`
    is_promoted: bool = False  # constructor property promotion (also becomes a Property)
    visibility: str = ""  # set only for promoted params ("public"/"private"/"protected")


@dataclass
class RawCall:
    name: str  # the callee's simple name (method/function name, or the created type)
    receiver: str | None = None
    """The receiver as a simple name: "this" for `$this->m()`; a property/var name for
    `$this->store->m()` ("store") or `$store->m()` ("store"); a class name for `Foo::m()`
    ("Foo") or one of "self"/"static"/"parent". None for a bare `foo()` or a receiver too
    complex to name. resolution.py uses it to prefer a receiver's declared type."""
    kind: str = "function"  # "function" | "method" | "static" | "new"


@dataclass
class RawMethod:
    name: str
    kind: str  # "ORDINARY" | "CONSTRUCTOR"
    visibility: str  # "public" | "private" | "protected" (PHP default: public)
    is_static: bool = False
    is_abstract: bool = False
    is_final: bool = False
    return_type: str | None = None
    parameters: list[RawParameter] = field(default_factory=list)
    calls: list[RawCall] = field(default_factory=list)
    source: str = ""
    doc: str = ""  # shared `doc` property: the leading comment above the declaration, cleaned
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawFunction:  # top-level (free) function
    name: str
    return_type: str | None = None
    parameters: list[RawParameter] = field(default_factory=list)
    calls: list[RawCall] = field(default_factory=list)
    source: str = ""
    doc: str = ""  # shared `doc` property: the leading comment above the declaration, cleaned
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawProperty:
    name: str  # without the leading `$`
    type: str
    visibility: str = "public"
    is_static: bool = False
    is_readonly: bool = False
    is_promoted: bool = False  # declared via constructor property promotion
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawConstant:
    name: str
    visibility: str = "public"
    value: str = ""  # value text as written
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawEnumCase:
    name: str
    value: str = ""  # backing value text ("" for pure enums)


@dataclass
class RawType:
    kind: str  # "Class" | "Interface" | "Trait" | "Enum"
    name: str
    namespace: str  # "" for the global namespace; backslash-separated otherwise ("App\\Models")
    is_abstract: bool = False
    is_final: bool = False
    superclass: str | None = None  # class only -- single base (simple name)
    interfaces: list[str] = field(default_factory=list)  # implemented (class/enum) or extended (interface)
    traits: list[str] = field(default_factory=list)  # `use TraitName;` in the body -> USES_TRAIT
    backing_type: str | None = None  # enum backing type ("string"/"int") if any
    methods: list[RawMethod] = field(default_factory=list)
    properties: list[RawProperty] = field(default_factory=list)
    constants: list[RawConstant] = field(default_factory=list)
    enum_cases: list[RawEnumCase] = field(default_factory=list)
    source: str = ""
    doc: str = ""  # shared `doc` property: the leading comment above the declaration, cleaned
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawUse:  # a top-level `use` import
    name: str  # imported fully-qualified name, e.g. "App\\Contracts\\Repository"
    alias: str | None = None  # `use X as Y` -> "Y"


@dataclass
class RawFile:
    path: str
    namespace: str = ""
    uses: list[RawUse] = field(default_factory=list)
    types: list[RawType] = field(default_factory=list)
    functions: list[RawFunction] = field(default_factory=list)
