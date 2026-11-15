"""Plain, picklable structures the file-based PL/SQL parser (source_analysis.
parse_declarations) produces per source file, before resolution.py turns them into
graph nodes/edges.

Picklable on purpose: git_sync.py parses files across a ProcessPoolExecutor keyed by
blob sha (exactly like the C# ingestor), so a parsed file has to survive being
pickled to a worker and cached to disk. Everything here is plain dataclasses of
str/int/bool/None/list/tuple -- no ANTLR context objects leak out of the parser.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RawParameter:
    name: str | None
    position: int
    direction: str | None  # "IN" | "OUT" | "IN OUT" | None (unspecified defaults to IN)
    data_type: str | None
    has_default: bool


@dataclass
class RawRoutine:
    name: str
    kind: str  # "PROCEDURE" | "FUNCTION"
    parameters: list[RawParameter] = field(default_factory=list)
    return_type: str | None = None  # FUNCTION only
    source: str | None = None
    doc: str = ""  # leading `--`/`/* */` documentation comment (see source_analysis._attach_docs)
    calls: list[str] = field(default_factory=list)
    table_refs: list[tuple[str, str]] = field(default_factory=list)  # (table_name, operation)


@dataclass
class RawPackage:
    name: str
    spec_source: str | None = None
    body_source: str | None = None
    doc: str = ""  # leading `--`/`/* */` documentation comment (see source_analysis._attach_docs)
    members: list[RawRoutine] = field(default_factory=list)


@dataclass
class RawTrigger:
    name: str
    table: str | None = None
    source: str | None = None
    doc: str = ""  # leading `--`/`/* */` documentation comment (see source_analysis._attach_docs)
    calls: list[str] = field(default_factory=list)
    table_refs: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class RawType:
    name: str
    kind: str | None = None


# -- SQL DDL (CREATE TABLE / VIEW / INDEX / SEQUENCE) --------------------------------


@dataclass
class RawColumn:
    name: str
    position: int
    data_type: str | None
    nullable: bool
    default_value: str | None
    is_primary_key: bool = False


@dataclass
class RawConstraint:
    name: str | None
    type: str  # "PRIMARY_KEY" | "UNIQUE" | "CHECK"
    columns: list[str] = field(default_factory=list)
    expression: str | None = None  # CHECK condition text


@dataclass
class RawForeignKey:
    name: str | None
    columns: list[str]  # constrained columns in this table
    ref_table: str
    ref_columns: list[str]


@dataclass
class RawTable:
    name: str
    columns: list[RawColumn] = field(default_factory=list)
    constraints: list[RawConstraint] = field(default_factory=list)
    foreign_keys: list[RawForeignKey] = field(default_factory=list)


@dataclass
class RawTableAlteration:
    """An `ALTER TABLE <table> ADD ...` -- columns/constraints/foreign keys added to a
    table that may have been created in this or another file. Merged onto the by-name
    table in resolution, so a schema spread across CREATE + ALTER scripts (the common
    Oracle layout) reconstructs fully from files, no database."""

    table: str
    columns: list[RawColumn] = field(default_factory=list)
    constraints: list[RawConstraint] = field(default_factory=list)
    foreign_keys: list[RawForeignKey] = field(default_factory=list)


@dataclass
class RawView:
    name: str
    definition: str | None = None
    reads: list[str] = field(default_factory=list)  # table names the view selects from


@dataclass
class RawIndexDef:
    name: str
    table: str
    columns: list[str] = field(default_factory=list)
    unique: bool = False


@dataclass
class RawSequence:
    name: str


@dataclass
class RawPlsqlFile:
    """Everything one source file contributed -- both PL/SQL code and SQL DDL. `path`
    is re-stamped by the caller to a repo-relative path (git_sync) or a
    directory-relative path (file ingestor) after parsing, matching how the C#
    RawFile carries its path."""

    path: str
    packages: list[RawPackage] = field(default_factory=list)
    routines: list[RawRoutine] = field(default_factory=list)  # standalone (non-packaged)
    triggers: list[RawTrigger] = field(default_factory=list)
    types: list[RawType] = field(default_factory=list)
    tables: list[RawTable] = field(default_factory=list)
    views: list[RawView] = field(default_factory=list)
    indexes: list[RawIndexDef] = field(default_factory=list)
    sequences: list[RawSequence] = field(default_factory=list)
    alterations: list[RawTableAlteration] = field(default_factory=list)
    statement_kinds: list[str] = field(default_factory=list)
    """Every top-level statement kind the grammar recognised in this file (the parse tree's
    unit_statement child rule names, e.g. "Create_table", "Create_materialized_view"). Used to
    report statements that parsed cleanly but this ingestor doesn't model yet -- so coverage
    gaps are logged rather than silently dropped. Only populated on the strict path."""

    parse_errors: int = 0
    """Best-effort (non-strict) only: how many syntax errors ANTLR recovered from while parsing
    this file. >0 means a genuine grammar gap (e.g. a non-literal parameter default the vendored
    grammar can't handle) was skipped -- the rest of the file was still extracted. Surfaced by
    the ingestor so grammar gaps are visible, not silent."""

    first_error: tuple[int, int] | None = None
    """(line, column) of the first recovered parse error, for the grammar-gap report."""

    error_samples: list[tuple[int, str]] = field(default_factory=list)
    """Best-effort only: (line, source-line-text) for each recovered grammar gap -- one per
    distinct line, capped -- so the grammar-gaps report shows the actual offending constructs
    (enough to fix each class without a re-run), not just a count."""

    timed_out: bool = False
    """Best-effort only: True if this file's parse exceeded --max-parse-seconds and was skipped
    (empty result) to keep the run bounded. Reported so skipped files are visible, not silent."""

    def is_empty(self) -> bool:
        return not (
            self.packages
            or self.routines
            or self.triggers
            or self.types
            or self.tables
            or self.views
            or self.indexes
            or self.sequences
            or self.alterations
        )
