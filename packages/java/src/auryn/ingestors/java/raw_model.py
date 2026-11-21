"""Picklable dataclasses produced by file_parser (phase-1, per-file, no shared state -- safe
under a ProcessPoolExecutor). No tree-sitter node ever crosses this boundary; resolution.py
turns these into graph nodes/edges once every file has been parsed.

Everything here is what a single tree-sitter parse of one .java file can know on its own --
names, types-as-written (text), modifiers, annotations, call *names*. Cross-file decisions
(does this call resolve to a project method? does a base name name a project type?) are made
later, in resolution.py, where all files are visible at once.

Modelled after the C# ingestor's raw_model (Java and C# are structural siblings), pared to
Java's constructs: no properties/events/delegates/structs; a class has a single `superclass`
plus a separate `interfaces` list (so EXTENDS vs IMPLEMENTS needs no naming heuristic, unlike
C#'s mixed base_list). line_start/line_end are 1-based inclusive, matching the C/Python
ingestors; 0 means not captured."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RawAnnotation:
    name: str
    arguments: list[str] = field(default_factory=list)


@dataclass
class RawParameter:
    name: str
    type: str  # type as written, e.g. "Store", "List<String>", "int"
    position: int
    is_varargs: bool = False  # `String... args`
    is_final: bool = False


@dataclass
class RawCall:
    name: str  # the callee's simple name (method_invocation name, or the created type)
    receiver: str | None = None
    """The receiver's own simple name when it's a bare identifier or `this` ("store" for
    `store.put()`, "this" for `this.save()`, None for a bare `foo()` or a receiver too complex
    to name -- a field_access or method chain). resolution.py uses it to prefer resolving
    against a field's declared type over a same-name coincidence."""
    is_new: bool = False  # `new Widget()` -> resolves against a constructor
    line: int = 0  # 1-based line of the callee identifier (method name, or the created type) -- what SCIP
    # anchors an occurrence to; feeds the deep pass's line-precise call-site gate. 0 = not captured.


@dataclass
class RawMethod:
    name: str
    kind: str  # "ORDINARY" | "CONSTRUCTOR"
    accessibility: str  # "public" | "private" | "protected" | "package"
    modifiers: list[str] = field(default_factory=list)  # static/final/abstract/synchronized/...
    return_type: str | None = None  # None for a constructor
    parameters: list[RawParameter] = field(default_factory=list)
    throws: list[str] = field(default_factory=list)
    annotations: list[RawAnnotation] = field(default_factory=list)
    calls: list[RawCall] = field(default_factory=list)
    source: str = ""
    doc: str = ""  # shared `doc` property: the leading comment above the declaration, cleaned
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawField:
    name: str
    type: str
    accessibility: str = "package"
    modifiers: list[str] = field(default_factory=list)  # static/final/volatile/transient
    annotations: list[RawAnnotation] = field(default_factory=list)
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawEnumMember:
    name: str
    arguments: list[str] = field(default_factory=list)


@dataclass
class RawType:
    kind: str  # "Class" | "Interface" | "Enum" | "Record"
    name: str
    package: str  # "" for the default package
    accessibility: str = "package"
    modifiers: list[str] = field(default_factory=list)
    type_parameters: list[str] = field(default_factory=list)
    superclass: str | None = None  # class only -- single base (simple name)
    interfaces: list[str] = field(default_factory=list)  # implemented (class/enum/record) or extended (interface)
    annotations: list[RawAnnotation] = field(default_factory=list)
    methods: list[RawMethod] = field(default_factory=list)
    fields: list[RawField] = field(default_factory=list)
    enum_members: list[RawEnumMember] = field(default_factory=list)
    record_components: list[RawParameter] = field(default_factory=list)  # Record only
    nested_types: list["RawType"] = field(default_factory=list)
    source: str = ""
    doc: str = ""  # shared `doc` property: the leading comment above the declaration, cleaned
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawImport:
    name: str  # dotted, e.g. "java.util.List" or "com.acme.other" (wildcard drops the .*)
    is_static: bool = False
    is_wildcard: bool = False


@dataclass
class RawFile:
    path: str
    package: str = ""
    imports: list[RawImport] = field(default_factory=list)
    types: list[RawType] = field(default_factory=list)
