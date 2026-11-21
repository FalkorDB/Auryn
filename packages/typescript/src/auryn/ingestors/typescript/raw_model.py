"""Picklable dataclasses produced by file_parser (phase-1, per-file, no shared state -- safe
under a ProcessPoolExecutor). No tree-sitter node ever crosses this boundary; resolution.py
turns these into graph nodes/edges once every file has been parsed.

Everything here is what a single tree-sitter parse of one JS/TS file can know on its own --
names, types-as-written (text), the bare type names an annotation mentions, modifiers, and
call/import *names*. Cross-file decisions (does this import resolve to a project module? is
this call a local function or a method on a known type?) are made later, in resolution.py,
where all files are visible at once.

line_start/line_end are 1-based inclusive line ranges, mirroring the C/Python ingestors so
consumers see the same location properties across languages. 0 means "not captured".
`type` is the annotation text exactly as written ("" when absent -- e.g. any JS binding);
`type_refs` are the bare type-identifier names within it (for REFERENCES resolution)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RawParameter:
    name: str
    type: str  # annotation as written, e.g. "Store", "number[]", "string | null"; "" if none
    position: int
    optional: bool = False  # `x?: T` or has a default
    is_rest: bool = False  # `...args: T[]`
    type_refs: list[str] = field(default_factory=list)  # bare type names the annotation mentions


@dataclass
class RawCall:
    name: str  # the callee's simple name (property/identifier at the call site)
    receiver: str | None = None
    """The receiver expression's own simple name when it's a bare identifier or `this`
    (e.g. "store" for `this.store.put()` collapses to receiver "store"? -- no: we record the
    *immediate* object's simple name: "this" for `this.save()`, "store" for `store.put()`,
    None for a bare `foo()` or a receiver too complex to name simply). resolution.py uses it
    to prefer resolving against a field/param's declared type over a name coincidence."""
    is_new: bool = False  # `new Widget()` -- resolves against a constructor/class
    http_url: str | None = None  # request URL/route when this is an HTTP-client call (fetch/axios/$.ajax…)
    http_method: str | None = None  # GET/POST/… when derivable (verb-named call or options); None -> GET at stitch
    line: int = 0  # 1-based line of the callee *identifier* (what SCIP anchors an occurrence to), for the
    # deep pass's line-precise call-site gate; 0 means "not captured"


@dataclass
class RawParameterized:
    """Shared shape of the two callable kinds (function + method). Not emitted directly."""

    name: str
    params: list[RawParameter] = field(default_factory=list)
    return_type: str = ""  # as written; "" if no annotation
    return_type_refs: list[str] = field(default_factory=list)
    is_async: bool = False
    calls: list[RawCall] = field(default_factory=list)
    """Every call/`new` in the body -- best-effort structural fact gathered while the AST is
    here; resolution.py turns one into a CALLS edge or drops it, unresolved."""
    source: str = ""
    doc: str = ""  # shared `doc` property: the leading comment above the declaration, cleaned
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawFunction(RawParameterized):
    kind: str = "FUNCTION"  # "FUNCTION" (function decl) | "ARROW" | "FUNCTION_EXPR"
    is_exported: bool = False


@dataclass
class RawMethod(RawParameterized):
    kind: str = "METHOD"  # "METHOD" | "CONSTRUCTOR" | "GETTER" | "SETTER"
    accessibility: str = "public"  # "public" | "private" | "protected"
    is_static: bool = False
    is_abstract: bool = False


@dataclass
class RawProperty:
    name: str
    type: str = ""  # as written; "" if none
    type_refs: list[str] = field(default_factory=list)
    accessibility: str = "public"
    is_static: bool = False
    is_readonly: bool = False
    is_optional: bool = False


@dataclass
class RawClass:
    name: str
    is_abstract: bool = False
    is_exported: bool = False
    type_parameters: list[str] = field(default_factory=list)
    extends: list[str] = field(default_factory=list)  # simple base names (TS: at most one)
    implements: list[str] = field(default_factory=list)  # simple interface names
    methods: list[RawMethod] = field(default_factory=list)
    properties: list[RawProperty] = field(default_factory=list)
    source: str = ""
    doc: str = ""  # shared `doc` property: the leading comment above the declaration, cleaned
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawInterface:
    name: str
    is_exported: bool = False
    type_parameters: list[str] = field(default_factory=list)
    extends: list[str] = field(default_factory=list)  # interfaces can extend many
    methods: list[RawMethod] = field(default_factory=list)  # method_signature (no body)
    properties: list[RawProperty] = field(default_factory=list)
    source: str = ""
    doc: str = ""  # shared `doc` property: the leading comment above the declaration, cleaned
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawEnum:
    name: str
    is_const: bool = False
    is_exported: bool = False
    members: list[str] = field(default_factory=list)
    source: str = ""
    doc: str = ""  # shared `doc` property: the leading comment above the declaration, cleaned
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawTypeAlias:
    name: str
    value: str  # aliased type as written
    is_exported: bool = False
    type_refs: list[str] = field(default_factory=list)  # bare type names the alias mentions
    source: str = ""
    doc: str = ""  # shared `doc` property: the leading comment above the declaration, cleaned
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawImport:
    module: str  # the module specifier exactly as written, e.g. "./store", "lib"
    kind: str  # "named" | "default" | "namespace" | "side_effect" | "reexport"
    names: list[str] = field(default_factory=list)  # imported/re-exported local names
    line_start: int = 0


@dataclass
class RawFile:
    path: str
    dialect: str = "ts"  # "ts" | "js" -- how much type info the file can carry
    is_declaration: bool = False  # a `.d.ts` file
    has_error: bool = False  # tree-sitter found a syntax error (broken/mid-edit code)
    imports: list[RawImport] = field(default_factory=list)
    classes: list[RawClass] = field(default_factory=list)
    interfaces: list[RawInterface] = field(default_factory=list)
    enums: list[RawEnum] = field(default_factory=list)
    type_aliases: list[RawTypeAlias] = field(default_factory=list)
    functions: list[RawFunction] = field(default_factory=list)
