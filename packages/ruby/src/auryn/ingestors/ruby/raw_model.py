"""Picklable dataclasses produced by file_parser (phase-1, per-file, no shared state -- safe
under a ProcessPoolExecutor). No tree-sitter node ever crosses this boundary; resolution.py
turns these into graph nodes/edges once every file has been parsed.

Everything here is what a single tree-sitter parse of one .rb file can know on its own -- names,
parameter shapes, call *names* + receiver shapes, mixin/superclass names as written, `require`
targets. Cross-file decisions (does a call resolve to a project method? does a `< Base`
superclass name a project class? does a `require_relative` land on a project file?) are made
later in resolution.py, where all files are visible.

Ruby is namespace-based: a file's top level, and every `module`/`class` inside it, are scopes
that carry the same nested item lists (classes/modules/methods/consts/fields). A class and a
module both open a namespace and hold members; they differ only in that a class may have a
superclass and be instantiated. A callable is a single `RawMethod` whether free (a top-level
`def`, resolution labels it Function) or bound (a `def` inside a class/module, labelled Method).
`is_singleton` marks a class/singleton method (`def self.x`, or a `def` inside `class << self`).

Ruby is DYNAMIC: there is no static type text on a parameter, a local, or an instance variable.
So parameters carry only a `kind` (required/optional/splat/keyword/...) -- never a type -- and
call resolution is name-based (see resolution.py). line_start/line_end are 1-based inclusive."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RawParameter:
    name: str
    kind: str  # "required"|"optional"|"splat"|"keyword"|"keyword_optional"|"hash_splat"|"block"
    position: int
    default: str | None = None  # source text of an optional/keyword default, else None


@dataclass
class RawCall:
    name: str  # the callee's method name ("super" for a bare/`super(..)` call)
    kind: str
    """How the callee was written, driving name-based resolution in resolution.py:
    "command"  -- receiverless (`foo`, `foo(x)`, `foo { }`): a self-method or top-level function.
    "self"     -- `self.foo`: a method on the enclosing class/module.
    "constant" -- `Const.foo` / `A::B.foo`: a class/module method; `qualifier` is the const path.
    "instance" -- `x.foo` / `@iv.foo`: dynamic receiver; `receiver` is the local/ivar name.
    "chain"    -- `a.b.foo` and other complex receivers: too dynamic to name -- dropped.
    "super"    -- `super` / `super(..)`.
    """
    receiver: str | None = None   # for "instance": the bare local/@ivar receiver text
    qualifier: str | None = None  # for "constant": the constant path as written ("A::B")


@dataclass
class RawMethod:
    name: str  # the method name; operator/`?`/`!`/setter (`x=`) names are kept verbatim
    kind: str  # "FUNCTION" (top-level def) | "METHOD" (def inside a class/module)
    is_singleton: bool = False  # class/singleton method (`def self.x` / `class << self` body)
    visibility: str = "public"  # "public" | "private" | "protected"
    parameters: list[RawParameter] = field(default_factory=list)
    calls: list[RawCall] = field(default_factory=list)
    signature: str = ""  # reconstructed `name(params)` -- the shared `signature` property
    source: str = ""
    doc: str = ""  # shared `doc` property: the leading `#`/`=begin..=end` comment, cleaned
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawField:
    """An attribute declared via `attr_accessor`/`attr_reader`/`attr_writer` -- the closest Ruby
    has to a declared field. `access` is reader/writer/accessor."""

    name: str
    access: str  # "reader" | "writer" | "accessor"
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawConst:
    name: str  # an uppercase-initial constant assignment (`MAX = 10`)
    value: str = ""  # the RHS source text, trimmed
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawInclude:
    name: str  # the mixed-in module name as written ("Comparable", "A::B")
    kind: str  # "include" | "prepend" | "extend"


@dataclass
class RawModule:
    name: str  # the name as written (may be a `::`-qualified path, e.g. "A::B")
    kind: str = "Module"  # "Module" | "Class"
    superclass: str | None = None  # Class only: the `< Base` name as written, else None
    includes: list[RawInclude] = field(default_factory=list)
    methods: list[RawMethod] = field(default_factory=list)
    fields: list[RawField] = field(default_factory=list)
    consts: list[RawConst] = field(default_factory=list)
    classes: list["RawModule"] = field(default_factory=list)
    modules: list["RawModule"] = field(default_factory=list)
    source: str = ""
    doc: str = ""  # shared `doc` property: the leading comment, cleaned
    line_start: int = 0
    line_end: int = 0


@dataclass
class RawRequire:
    target: str  # the string literal passed to require/require_relative
    kind: str  # "require" | "require_relative"


@dataclass
class RawFile:
    """A parsed .rb file. Its own item lists are the file's top-level scope; `classes`/`modules`
    hold the namespaces declared in it. Shares the item-list field names with RawModule so
    file_parser's collect_scope fills either interchangeably."""

    path: str
    requires: list[RawRequire] = field(default_factory=list)
    classes: list[RawModule] = field(default_factory=list)
    modules: list[RawModule] = field(default_factory=list)
    methods: list[RawMethod] = field(default_factory=list)  # top-level `def` -> Function
    consts: list[RawConst] = field(default_factory=list)
    fields: list[RawField] = field(default_factory=list)  # top-level attr_* (rare)
