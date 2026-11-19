"""Plain, picklable dataclasses forming the phase-1/phase-2 boundary of the `ast`
fallback engine (mirrors ingestors/csharp/raw_model.py).

file_parser.parse_module() builds these from a stdlib `ast` tree, one per file. No `ast`
node ever escapes the parser -- only these dataclasses do -- and resolution.py (the
project-wide second pass) is the only reader.

These deliberately carry *syntactic* facts only (plus name positions, so a semantic
oracle can be queried at them). resolution.py binds names to project-local declarations
where it statically can; with no oracle (the `ast` engine) everything unannotated is
`unresolved`, while the `jedi` engine passes an oracle that infers types and resolves
receiver-typed calls at those positions.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RawImport:
    """A binding introduced by an import statement, normalized to (local name -> target).

    `from a.b import C as D`  -> local="D", target="a.b.C", is_module=False
    `import a.b as m`         -> local="m", target="a.b",   is_module=True
    `import a`                -> local="a", target="a",     is_module=True
    Relative imports are resolved to absolute `target` at parse time when possible.
    """

    local: str
    target: str
    is_module: bool


@dataclass
class RawParameter:
    name: str
    annotation: str | None  # source text of the annotation, or None if unannotated
    kind: str  # positional_only | positional_or_keyword | keyword_only | var_positional | var_keyword
    position: int
    has_default: bool
    name_line: int = 0  # 1-based line / 0-based col of the name, for a semantic oracle to query
    name_col: int = 0


@dataclass
class RawAttribute:
    name: str
    annotation: str | None
    defined_in: str  # "class_body" | the method name (e.g. "__init__") for a self.x assignment
    is_classvar: bool
    name_line: int = 0
    name_col: int = 0


@dataclass
class RawCall:
    """A call site, best-effort. The ast engine binds `name` to a project-local function
    only when it can statically (a bare `name()` via imports/same-module, or
    `alias.name()` where alias is an imported project module). The Jedi engine instead
    resolves the callee at (line, col) via inference, which also reaches receiver-typed
    calls (`conn.execute()`)."""

    name: str  # bare Name id, or the attribute name for `receiver.name()`
    receiver: str | None  # the receiver's simple name for `receiver.name()`, else None
    is_simple_name: bool  # True when the callee is a bare Name (not an attribute access)
    line: int = 0  # position of the callee name, for the Jedi oracle
    col: int = 0


@dataclass
class RawFunction:
    name: str
    params: list[RawParameter] = field(default_factory=list)
    returns: str | None = None  # source text of the -> annotation, or None
    decorators: list[str] = field(default_factory=list)
    is_async: bool = False
    is_generator: bool = False
    line_start: int = 0
    line_end: int = 0
    calls: list[RawCall] = field(default_factory=list)
    self_attrs: list[RawAttribute] = field(default_factory=list)  # methods only: self.x = ... targets
    source: str = ""  # the def's source text (shared `source` property, as C/C#/PL-SQL store it)
    doc: str = ""  # shared `doc` property: leading `#` comment block above the def, cleaned


@dataclass
class RawClass:
    name: str
    bases: list[str] = field(default_factory=list)  # source text of each base expression
    decorators: list[str] = field(default_factory=list)
    line_start: int = 0
    line_end: int = 0
    methods: list[RawFunction] = field(default_factory=list)
    source: str = ""  # the class's source text (shared `source` property)
    doc: str = ""  # shared `doc` property: leading `#` comment block above the class, cleaned
    class_attrs: list[RawAttribute] = field(default_factory=list)
    nested: list["RawClass"] = field(default_factory=list)


@dataclass
class RawModule:
    path: str  # repo-relative
    dotted: str
    is_package: bool = False  # True for an __init__.py (represents its Package node)
    imports: list[RawImport] = field(default_factory=list)
    classes: list[RawClass] = field(default_factory=list)
    functions: list[RawFunction] = field(default_factory=list)
    calls: list[RawCall] = field(default_factory=list)  # module-scope (top-level) call sites
    # Package re-exports (`__init__.py` only): every `from .x import Y [as Z]` the package exposes as
    # `pkg.Z`, incl. those inside `if TYPE_CHECKING:` and a lazy `_dynamic_imports` table. Kept apart
    # from `imports` (which drives call resolution) -- used only to publish linkage aliases so a
    # consumer's `from pkg import Z` placeholder can link to Z's real definition (cross-repo).
    reexports: list[RawImport] = field(default_factory=list)
    parse_ok: bool = True  # False if the file failed to parse (SyntaxError) -- see file_parser
