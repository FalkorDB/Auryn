"""Plain, picklable dataclasses forming the phase-1/phase-2 boundary (see the C#
ingestor plan's "Extraction pipeline" section).

file_parser.parse_file() builds these directly from tree-sitter nodes, one process
per file. Once built, no tree-sitter node ever crosses the ProcessPoolExecutor
boundary -- only these plain dataclasses do, which is what makes them safely
picklable. resolution.py (running back in the main process once every file is
parsed) is the only code that reads them.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RawAttribute:
    name: str
    arguments: list[str] = field(default_factory=list)


@dataclass
class RawParameter:
    name: str
    type: str
    direction: str  # "VALUE" | "REF" | "OUT" | "IN" | "PARAMS"
    position: int
    has_default: bool
    attributes: list[RawAttribute] = field(default_factory=list)


@dataclass
class RawCall:
    name: str
    receiver: str | None = None
    """The receiver expression's own simple name, when it's a bare identifier or
    `this` (e.g. "_service" for `_service.GetById()`, "this" for `this.GetById()`,
    None for a bare `GetById()` call or a receiver too complex to name simply, e.g.
    chained member access). resolution.py uses this to prefer resolving against the
    receiver's declared field/property type over a same-type-name coincidence --
    see its docstring for why that matters."""
    http_url: str | None = None  # request URL/route when this is an HttpClient call (ADR 0006)
    http_method: str | None = None  # GET/POST/… derived from the HttpClient method name


@dataclass
class RawMethod:
    name: str
    kind: str  # "ORDINARY" | "CONSTRUCTOR" | "DESTRUCTOR" | "OPERATOR"
    accessibility: str
    modifiers: list[str] = field(default_factory=list)
    return_type: str | None = None
    parameters: list[RawParameter] = field(default_factory=list)
    attributes: list[RawAttribute] = field(default_factory=list)
    source: str = ""
    doc: str = ""
    """Leading documentation comment (incl. `///` XML docs), markers stripped -- the
    natural-language half of the `doc + source` document the embedding layer embeds.
    See core.docstrings and docs/graph-model.md's shared `doc` property."""
    calls: list[RawCall] = field(default_factory=list)
    """Every call invoked in this method's body -- best-effort structural fact
    gathered while we still have the AST; resolution.py is what turns one into an
    actual DEPENDS_ON edge (or drops it, unresolved)."""


@dataclass
class RawProperty:
    name: str
    accessibility: str
    type: str
    has_getter: bool
    has_setter: bool
    is_auto_implemented: bool
    is_static: bool = False
    attributes: list[RawAttribute] = field(default_factory=list)


@dataclass
class RawField:
    name: str
    accessibility: str
    type: str
    is_readonly: bool = False
    is_const: bool = False
    is_static: bool = False
    attributes: list[RawAttribute] = field(default_factory=list)


@dataclass
class RawEvent:
    name: str
    accessibility: str
    type: str
    is_static: bool = False
    attributes: list[RawAttribute] = field(default_factory=list)


@dataclass
class RawEnumMember:
    name: str
    value: str | None = None


@dataclass
class RawType:
    kind: str  # "Class" | "Interface" | "Struct" | "Enum" | "Record" | "Delegate"
    name: str
    namespace: str  # "" for the global namespace
    accessibility: str = "internal"
    modifiers: list[str] = field(default_factory=list)
    type_parameters: list[str] = field(default_factory=list)
    base_list: list[str] = field(default_factory=list)
    """Simple names only (generic args/qualifiers already stripped in file_parser,
    where the AST is still available) -- unresolved as EXTENDS vs IMPLEMENTS until
    resolution.py's project-wide type index exists."""
    is_value_type: bool = False  # "record struct" / "readonly record struct"
    underlying_type: str | None = None  # Enum only
    return_type: str | None = None  # Delegate only
    positional_parameters: list[RawParameter] = field(default_factory=list)  # Record only
    attributes: list[RawAttribute] = field(default_factory=list)
    source: str = ""
    doc: str = ""
    """Leading documentation comment (incl. `///` XML docs), markers stripped -- see
    RawMethod.doc / core.docstrings for the shared `doc` property."""
    nested_types: list[RawType] = field(default_factory=list)
    methods: list[RawMethod] = field(default_factory=list)
    properties: list[RawProperty] = field(default_factory=list)
    fields: list[RawField] = field(default_factory=list)
    events: list[RawEvent] = field(default_factory=list)
    enum_members: list[RawEnumMember] = field(default_factory=list)


@dataclass
class RawFile:
    path: str
    using_namespaces: list[str] = field(default_factory=list)
    types: list[RawType] = field(default_factory=list)
