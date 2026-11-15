"""Parse Oracle DDL files into the shared Raw* model using **sqlglot** -- pure-Python, no ANTLR
runtime, ~15x faster on the view-heavy DDL that dominates a schema export, and higher-coverage on
modern Oracle SQL (it parses LISTAGG ... ON OVERFLOW, OUTER APPLY, INCREMENT BY -1, ... which the
vendored ANTLR grammar reports as gaps).

sqlglot has two blind spots we handle here so coverage never regresses versus ANTLR:
  * CREATE INDEX -> sqlglot returns an opaque `Command`; we regex it (index DDL is simple/regular).
  * Some Oracle *physical* storage clauses (USING INDEX TABLESPACE, SEGMENT CREATION, STORAGE(...))
    -> sqlglot raises ParseError. We surface that as `SqlglotUnsupported` so the caller can fall
    back to the ANTLR parser for just that file. The result is the UNION of both parsers' coverage.

Output is the same raw_model.RawPlsqlFile the ANTLR parser produces, so resolution.py is unchanged.
Identifiers are UPPER-cased to match Oracle's catalog (and the ANTLR path / live reflection).
"""

from __future__ import annotations

import logging
import re

import sqlglot
from sqlglot import exp

# sqlglot warns ("... contains unsupported syntax. Falling back to parsing as a 'Command'") on every
# CREATE INDEX -- which we handle on purpose via the regex below. On a real export that's one warning
# per index (thousands); silence sqlglot's own logger since we surface real problems ourselves.
logging.getLogger("sqlglot").setLevel(logging.ERROR)

from auryn.ingestors.plsql.raw_model import (
    RawColumn,
    RawConstraint,
    RawForeignKey,
    RawIndexDef,
    RawPlsqlFile,
    RawSequence,
    RawTable,
    RawTableAlteration,
    RawView,
)


class SqlglotUnsupported(Exception):
    """sqlglot could not parse this file's DDL -- the caller should fall back to the ANTLR parser."""


_READ = "oracle"

# CREATE [UNIQUE|BITMAP] INDEX [schema.]name ON [schema.]table (col, ...). Used only for the
# statements sqlglot hands back as an opaque Command.
_CREATE_INDEX_RE = re.compile(
    r"\bCREATE\s+(?P<mod>(?:UNIQUE|BITMAP)\s+)?INDEX\s+"
    r'(?:"?\w+"?\.)?"?(?P<name>\w+)"?\s+ON\s+'
    r'(?:"?\w+"?\.)?"?(?P<table>\w+)"?\s*\((?P<cols>.*?)\)',
    re.IGNORECASE | re.DOTALL,
)


def parse_ddl_file(source: str, path: str = "") -> RawPlsqlFile:
    """Parse a file of Oracle DDL into a RawPlsqlFile. Raises `SqlglotUnsupported` if sqlglot can't
    parse it (caller falls back to ANTLR)."""
    result = RawPlsqlFile(path=path)
    try:
        statements = sqlglot.parse(source, read=_READ)
    except sqlglot.errors.ParseError as error:
        raise SqlglotUnsupported(str(error)) from error
    for statement in statements:
        if statement is not None:
            _dispatch(statement, result)
    return result


def _dispatch(statement: exp.Expression, result: RawPlsqlFile) -> None:
    if isinstance(statement, exp.Create):
        kind = (statement.args.get("kind") or "").upper()
        if kind == "TABLE":
            result.tables.append(_table(statement))
        elif kind == "VIEW":
            result.views.append(_view(statement))
        elif kind == "SEQUENCE":
            result.sequences.append(_sequence(statement))
        # other CREATE kinds (packages, procedures, ...) aren't schema-catalog objects -> ignored
    elif isinstance(statement, exp.Alter):
        result.alterations.append(_alter(statement))
    elif isinstance(statement, exp.Command):
        index = _index_from_command(statement)
        if index is not None:
            result.indexes.append(index)
    # exp.Comment, grants, synonyms, etc. -> not modeled here, ignored


# -- names / helpers ----------------------------------------------------------------


def _name(node: exp.Expression | None) -> str:
    """Uppercased identifier text of a named node (Identifier/Table/Column/Ordered/...)."""
    if node is None:
        return ""
    if isinstance(node, exp.Ordered):  # an index/pk column with ASC/DESC
        node = node.this
    name = node.name if node.name else node.sql(dialect=_READ)
    return name.upper()


def _ident_names(expressions) -> list[str]:
    return [_name(e) for e in expressions]


def _defined_table(node: exp.Expression) -> exp.Expression:
    """The Table a Create/Alter targets. A column list (table DDL, or a view with an explicit
    column list) wraps the Table in a Schema node; unwrap it so the name isn't schema-qualified."""
    return node.this if isinstance(node, exp.Schema) else node


def _object_name(create: exp.Expression) -> str:
    return _name(_defined_table(create.this))


# -- CREATE TABLE -------------------------------------------------------------------


def _table(create: exp.Create) -> RawTable:
    schema = create.this  # exp.Schema(this=Table, expressions=[ColumnDef | constraint ...])
    table = RawTable(name=_object_name(create))
    expressions = schema.expressions if isinstance(schema, exp.Schema) else []
    _absorb_columns_and_constraints(expressions, table)
    return table


def _absorb_columns_and_constraints(expressions, target) -> None:
    """Fill `target` (a RawTable or RawTableAlteration -- both carry columns/constraints/
    foreign_keys) from a list of ColumnDefs and constraint nodes. Primary-key columns from inline
    and out-of-line declarations are merged into one PRIMARY_KEY constraint, matching the ANTLR
    path."""
    pk = RawConstraint(name=None, type="PRIMARY_KEY", columns=[])
    position = len(target.columns)
    for node in expressions:
        if isinstance(node, exp.ColumnDef):
            position += 1
            target.columns.append(_column(node, position, target, pk))
        elif isinstance(node, exp.Constraint):  # named: CONSTRAINT <name> <PK|FK|UNIQUE|CHECK>
            for inner in node.expressions:
                _out_of_line_constraint(inner, node.name, target, pk)
        else:  # unnamed table-level constraint
            _out_of_line_constraint(node, None, target, pk)

    pk_set = {c.upper() for c in pk.columns}
    for column in target.columns:
        if column.name.upper() in pk_set:
            column.is_primary_key = True
    if pk.columns:
        target.constraints.insert(0, pk)


def _column(coldef: exp.ColumnDef, position: int, target, pk: RawConstraint) -> RawColumn:
    name = _name(coldef.this)
    data_type = coldef.kind.sql(dialect=_READ) if coldef.kind else None
    not_null = False
    default = None
    for constraint in coldef.constraints:
        kind = constraint.kind
        if isinstance(kind, exp.PrimaryKeyColumnConstraint):
            pk.columns.append(name)
        elif isinstance(kind, exp.NotNullColumnConstraint):
            not_null = True
        elif isinstance(kind, exp.DefaultColumnConstraint):
            default = kind.this.sql(dialect=_READ) if kind.this is not None else None
        elif isinstance(kind, exp.UniqueColumnConstraint):
            target.constraints.append(RawConstraint(name=None, type="UNIQUE", columns=[name]))
        elif isinstance(kind, exp.CheckColumnConstraint):
            target.constraints.append(
                RawConstraint(name=None, type="CHECK", expression=kind.this.sql(dialect=_READ))
            )
        elif isinstance(kind, exp.Reference):
            target.foreign_keys.append(_reference_fk(None, [name], kind))
    return RawColumn(
        name=name, position=position, data_type=data_type, nullable=not not_null, default_value=default
    )


def _out_of_line_constraint(node: exp.Expression, name: str | None, target, pk: RawConstraint) -> None:
    if isinstance(node, exp.PrimaryKey):
        pk.columns.extend(_ident_names(node.expressions))
        pk.name = pk.name or (name.upper() if name else None)
    elif isinstance(node, exp.ForeignKey):
        target.foreign_keys.append(_foreign_key(name, node))
    elif isinstance(node, exp.UniqueColumnConstraint):
        target.constraints.append(
            RawConstraint(name=name.upper() if name else None, type="UNIQUE", columns=_constraint_columns(node))
        )
    elif isinstance(node, (exp.CheckColumnConstraint, exp.Check)):
        target.constraints.append(
            RawConstraint(
                name=name.upper() if name else None,
                type="CHECK",
                expression=node.this.sql(dialect=_READ) if node.this is not None else None,
            )
        )


def _constraint_columns(node: exp.Expression) -> list[str]:
    """Column names inside a UNIQUE/PK paren list -- they reflect as Columns or bare Identifiers."""
    columns = [_name(c) for c in node.find_all(exp.Column)]
    if columns:
        return columns
    return [_name(i) for i in node.find_all(exp.Identifier)]


def _foreign_key(name: str | None, fk: exp.ForeignKey) -> RawForeignKey:
    reference = fk.args.get("reference")
    return _reference_fk(name, _ident_names(fk.expressions), reference)


def _reference_fk(name: str | None, columns: list[str], reference: exp.Reference) -> RawForeignKey:
    ref_schema = reference.this  # exp.Schema(this=Table, expressions=[ref cols]) or bare Table
    ref_table = ref_schema.this if isinstance(ref_schema, exp.Schema) else ref_schema
    ref_columns = _ident_names(ref_schema.expressions) if isinstance(ref_schema, exp.Schema) else []
    return RawForeignKey(
        name=name.upper() if name else None,
        columns=columns,
        ref_table=_name(ref_table),
        ref_columns=ref_columns,
    )


# -- CREATE VIEW / SEQUENCE / ALTER / INDEX -----------------------------------------


def _view(create: exp.Create) -> RawView:
    name = _object_name(create)
    select = create.expression
    reads: list[str] = []
    if select is not None:
        seen = set()
        for table in select.find_all(exp.Table):
            table_name = _name(table)
            if table_name and table_name != name and table_name not in seen:
                seen.add(table_name)
                reads.append(table_name)
    return RawView(name=name, definition=create.sql(dialect=_READ), reads=reads)


def _sequence(create: exp.Create) -> RawSequence:
    return RawSequence(name=_object_name(create))


def _alter(alter: exp.Alter) -> RawTableAlteration:
    alteration = RawTableAlteration(table=_name(alter.this))
    for action in alter.args.get("actions") or []:
        if isinstance(action, exp.Schema):  # ALTER TABLE t ADD (col, ...)
            _absorb_columns_and_constraints(action.expressions, alteration)
        elif isinstance(action, exp.AddConstraint):  # ALTER TABLE t ADD CONSTRAINT ...
            _absorb_columns_and_constraints(action.expressions, alteration)
    return alteration


def _index_from_command(command: exp.Command) -> RawIndexDef | None:
    match = _CREATE_INDEX_RE.search(command.sql(dialect=_READ))
    if match is None:
        return None
    return RawIndexDef(
        name=match.group("name").upper(),
        table=match.group("table").upper(),
        columns=_index_columns(match.group("cols")),
        unique=bool(match.group("mod") and "UNIQUE" in match.group("mod").upper()),
    )


def _index_columns(cols_text: str) -> list[str]:
    """Split a top-level comma list, keeping plain identifier columns and dropping function-based
    members (e.g. UPPER(name)) -- which don't map to a Column node anyway, matching the ANTLR path."""
    parts: list[str] = []
    depth = 0
    current = ""
    for char in cols_text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)

    columns: list[str] = []
    for part in parts:
        token = part.strip().strip('"')
        if not token or "(" in token:  # empty or a function/expression member
            continue
        token = token.split()[0]  # drop trailing ASC/DESC
        if re.fullmatch(r"\w+", token):
            columns.append(token.upper())
    return columns
