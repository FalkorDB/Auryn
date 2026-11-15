"""Picklable dataclasses produced by file_parser (phase-1, per-file, no shared state -- safe
under a ProcessPoolExecutor). No tree-sitter node ever crosses this boundary; resolution.py
turns these into graph nodes/edges once every file has been parsed.

Everything here is what a single tree-sitter parse of one .kt/.kts file can know on its own --
names, types-as-written (text), modifiers, annotations, call *names*. Cross-file decisions (does
this call resolve to a project function/method? does a base name name a project type?) are made
later, in resolution.py, where all files are visible at once.

Modelled after the Java ingestor's raw_model (Kotlin and Java are JVM structural siblings), with
two Kotlin realities the Java model lacks: (1) top-level `fun`/`val`/`var` (a file carries free
`RawFunction`s and property `RawField`s alongside its types -- like the Rust ingestor's free
functions), and (2) a single `superclass` (the delegation-specifier written with a constructor
call, `Base()`) split from the `interfaces` list (bare delegation specifiers) so EXTENDS vs
IMPLEMENTS needs no naming heuristic. line_start/line_end are 1-based inclusive; 0 means not
captured."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RawAnnotation:
    name: str
    arguments: list[str] = field(default_factory=list)


@dataclass
class RawParameter:
    name: str
    type: str  # type as written, e.g. "Store", "List<String>", "Int", "String?"
    position: int
    is_vararg: bool = False  # `vararg rest: Int`
    has_default: bool = False  # `b: String = "x"`


@dataclass
class RawCall:
    name: str  # the callee's simple name (call/navigation member, or a constructor's type name)
    receiver: str | None = None
    """The receiver's own simple name when it's a bare identifier or `this` ("store" for
    `store.put()`, "this" for `this.save()`, None for a bare `foo()`/`Foo()` or a receiver too
    complex to name -- a navigation chain, an index, ...). resolution.py uses it to prefer
    resolving against a field/param's declared type over a same-name coincidence. Kotlin has no
    `new`: a receiver-less capitalized call that names a project type is resolved as that type's
    constructor by resolution.py (no is_new flag needed)."""


@dataclass
class RawFunction:
    """A callable, whether a top-level `fun` (resolution labels it Function) or a member `fun`/
    constructor (Method). `kind` distinguishes an ordinary function from a constructor."""

    name: str  # a constructor uses its owning type's simple name (matches the Java ingestor)
    kind: str  # "ORDINARY" | "CONSTRUCTOR"
    accessibility: str  # "public" | "private" | "protected" | "internal"
    modifiers: list[str] = field(default_factory=list)  # abstract/open/override/suspend/inline/...
    return_type: str | None = None  # None for a constructor
    parameters: list[RawParameter] = field(default_factory=list)
    annotations: list[RawAnnotation] = field(default_factory=list)
    calls: list[RawCall] = field(default_factory=list)
    source: str = ""
    doc: str = ""  # shared `doc` property: the leading comment above the declaration, cleaned
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawField:
    """A property (`val`/`var`), whether a class member, a primary-constructor `val`/`var`
    parameter (Kotlin promotes those to properties), or a top-level property."""

    name: str
    type: str
    accessibility: str = "public"  # Kotlin default is public
    modifiers: list[str] = field(default_factory=list)  # const/lateinit/override/...
    is_mutable: bool = False  # `var` (True) vs `val` (False)
    annotations: list[RawAnnotation] = field(default_factory=list)
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawEnumMember:
    name: str
    arguments: list[str] = field(default_factory=list)


@dataclass
class RawType:
    kind: str  # "Class" | "Interface" | "Enum"
    name: str
    package: str  # "" for the default (no) package
    accessibility: str = "public"
    modifiers: list[str] = field(default_factory=list)  # abstract/open/sealed/data/enum/inner/...
    type_parameters: list[str] = field(default_factory=list)
    superclass: str | None = None  # class/object only -- the delegation specifier with a ctor call
    interfaces: list[str] = field(default_factory=list)  # implemented (class/enum/object) or extended (interface)
    is_object: bool = False  # `object`/`companion object` (a singleton), modelled as a Class
    is_companion: bool = False
    is_data: bool = False  # `data class`
    methods: list[RawFunction] = field(default_factory=list)  # member funs + constructors
    fields: list[RawField] = field(default_factory=list)  # properties (incl. primary-ctor val/var)
    enum_members: list[RawEnumMember] = field(default_factory=list)
    annotations: list[RawAnnotation] = field(default_factory=list)
    nested_types: list["RawType"] = field(default_factory=list)
    source: str = ""
    doc: str = ""  # shared `doc` property: the leading comment above the declaration, cleaned
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawImport:
    name: str  # dotted, e.g. "com.acme.model.Store" (wildcard drops the trailing `.*`)
    is_wildcard: bool = False
    alias: str | None = None  # `import a.b.C as D` -> "D"


@dataclass
class RawFile:
    path: str
    package: str = ""
    imports: list[RawImport] = field(default_factory=list)
    types: list[RawType] = field(default_factory=list)
    functions: list[RawFunction] = field(default_factory=list)  # top-level `fun`s
    properties: list[RawField] = field(default_factory=list)  # top-level `val`/`var`s
