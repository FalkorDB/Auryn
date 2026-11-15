"""Best-effort extraction of calls and table references from PL/SQL source, via the
vendored ANTLR4 grammar (see grammar/__init__.py for provenance/regeneration steps).

The grammar's own upstream README admits it's "ambiguous, but generally performs
well" -- parse_unit() never raises. Source that fails to parse just yields an empty
ParsedUnit and a logged warning; the caller falls back to Oracle's own (coarser)
ALL_DEPENDENCIES for that object rather than losing the whole ingestion run.

Scope: this only catches calls made as direct statements (`log_greeting(p_name);`)
and table references in SELECT/INSERT/UPDATE/DELETE -- function calls embedded inside
expressions (`v := greet_count(x);`) and MERGE statements aren't walked. Both still
show up via the coarser catalog-derived DEPENDS_ON edges, just without the `operation`/
parsed-level detail.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field

from antlr4 import CommonTokenStream, InputStream
from antlr4.atn.PredictionMode import PredictionMode
from antlr4.error.ErrorListener import ErrorListener
from antlr4.error.ErrorStrategy import BailErrorStrategy

from auryn.core.docstrings import clean_comment, leading_comment_from_source
from auryn.ingestors.plsql.diagnostics import SqlSyntaxError, StrictHaltListener
from auryn.ingestors.plsql.grammar.PlSqlLexer import PlSqlLexer
from auryn.ingestors.plsql.grammar.PlSqlParser import PlSqlParser
from auryn.ingestors.plsql.grammar.PlSqlParserVisitor import PlSqlParserVisitor
from auryn.ingestors.plsql.raw_model import (
    RawColumn,
    RawConstraint,
    RawForeignKey,
    RawIndexDef,
    RawPackage,
    RawParameter,
    RawPlsqlFile,
    RawRoutine,
    RawSequence,
    RawTable,
    RawTableAlteration,
    RawTrigger,
    RawType,
    RawView,
)

logger = logging.getLogger(__name__)

# Optional compiled accelerator (the `auryn-plsql-accel` wheel, built from tools/plsql-accel):
# it parses AND extracts entirely in C++ (ANTLR C++ runtime over the SAME pinned grammar), returning
# just the small RawPlsqlFile as JSON -- the parse tree never crosses into Python. That's what makes
# it a real speedup rather than the ~2.6x a tree-translating accelerator gives. When the wheel isn't
# installed this is None and we parse in pure Python exactly as before: a drop-in speedup, never a
# hard dependency. Used only by parse_declarations (the file ingestor's hot path); the strict/
# diagnostic path and parse_unit stay on the Python runtime.
try:
    import plsql_accel as _accel
except ImportError:  # pragma: no cover - only present when the optional accelerator wheel is installed
    _accel = None


def accelerator_available() -> bool:
    """True when the compiled C++ accelerator (`plsql_accel`) is importable and will be used by
    parse_declarations' best-effort path. Read live (not a cached constant) so tests/benchmarks
    that null out `_accel` are reflected. Note: the strict and parse_unit paths never use it."""
    return _accel is not None


@dataclass(frozen=True)
class TableRef:
    name: str
    operation: str  # "SELECT" | "INSERT" | "UPDATE" | "DELETE"


@dataclass
class ParsedUnit:
    calls: list[str] = field(default_factory=list)
    table_refs: list[TableRef] = field(default_factory=list)


class _RaisingErrorListener(ErrorListener):
    def syntaxError(self, recognizer, offendingSymbol, line, column, msg, e):
        raise ValueError(f"line {line}:{column}: {msg}")


class _CallsAndTableRefsVisitor(PlSqlParserVisitor):
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.table_refs: list[TableRef] = []

    def visitCall_statement(self, ctx):
        names = ctx.routine_name()
        if names:
            self.calls.append(_last_identifier(names[-1].getText()))
        return self.visitChildren(ctx)

    def visitTableview_name(self, ctx):
        operation = _classify_table_ref_context(ctx.parentCtx)
        if operation is not None:
            self.table_refs.append(TableRef(name=_last_identifier(ctx.getText()), operation=operation))
        return self.visitChildren(ctx)


class _PackageMemberSourceVisitor(PlSqlParserVisitor):
    """Slices each individual PROCEDURE/FUNCTION declaration's own text out of a
    parsed package body. ALL_SOURCE only ever gives the whole package body as one
    blob (no per-member row) -- this is the only way to get an individual member's
    own source rather than the whole package's."""

    def __init__(self, source: str) -> None:
        self._source = source
        self.members: dict[tuple[str, tuple[str, ...]], str] = {}

    def visitPackage_obj_body(self, ctx):
        body_ctx = ctx.procedure_body() or ctx.function_body()
        if body_ctx is not None:
            name = body_ctx.identifier().getText().upper()
            # Keyed by (name, parameter types), not just name, so overloaded members
            # (same name, different signature) don't clobber each other.
            param_types = tuple(p.type_spec().getText().upper() for p in body_ctx.parameter())
            self.members[(name, param_types)] = self._source[body_ctx.start.start : body_ctx.stop.stop + 1]
        return self.visitChildren(ctx)


def _classify_table_ref_context(ctx) -> str | None:
    """Walks up from a tableview_name node to find the nearest ancestor rule that
    tells us *how* the table is being used. tableview_name is the one leaf rule that
    always identifies a table name in this grammar regardless of how deeply nested
    the surrounding join/subquery structure is, so classifying by ancestor type here
    is more robust than trying to precisely mirror every intermediate rule ourselves."""
    while ctx is not None:
        rule_name = type(ctx).__name__
        if rule_name == "Update_statementContext":
            return "UPDATE"
        if rule_name == "Delete_statementContext":
            return "DELETE"
        if rule_name in ("Single_table_insertContext", "Insert_into_clauseContext"):
            return "INSERT"
        if rule_name == "From_clauseContext":
            return "SELECT"
        ctx = ctx.parentCtx
    return None


def _text(node_or_list) -> str | None:
    """getText() off an ANTLR accessor that may return a single context or a list
    (a rule appearing more than once in its parent -- e.g. a package/routine name
    that reappears after END -- generates a list-returning accessor). Takes the
    first occurrence."""
    if isinstance(node_or_list, list):
        node_or_list = node_or_list[0] if node_or_list else None
    return node_or_list.getText() if node_or_list is not None else None


def _last_identifier(text: str) -> str:
    """routine_name/tableview_name text may be schema- or package-qualified (e.g.
    "SCHEMA.PKG.PROC" or "SCHEMA.TABLE") -- the segment after the last dot is the
    actual object name, matching the SQL ingestor's own id scheme."""
    return text.rsplit(".", 1)[-1].upper()


class _SLLBail(Exception):
    """Raised (from the stage-1 error listener) to abandon the fast SLL parse and retry under
    full LL -- never escapes _parse."""


class _SLLBailListener(ErrorListener):
    def syntaxError(self, recognizer, offendingSymbol, line, column, msg, e):
        raise _SLLBail(msg)


_SLL_BAIL_LISTENER = _SLLBailListener()


def _fast_parse(source: str, rule: str):
    """Stage 1: parse under the SLL prediction mode with a bail-on-first-error strategy. SLL is
    near-linear where the default LL(*) can blow up (expression-heavy PL/SQL -- the CatsAndCalcs
    files are the pathological case: a single ~500-line calc package took ~30s under LL, <0.1s
    here). Returns the tree, or None if SLL can't parse it -- in which case the caller re-parses
    under full LL. This is the standard ANTLR two-stage strategy, and it's failure-preserving:
    a clean SLL parse yields the same tree LL would; anything SLL can't decide is retried under
    LL, so no valid file is rejected and no invalid file is accepted."""
    lexer = PlSqlLexer(InputStream(source))
    lexer.removeErrorListeners()
    lexer.addErrorListener(_SLL_BAIL_LISTENER)
    parser = PlSqlParser(CommonTokenStream(lexer))
    parser.removeErrorListeners()
    parser.addErrorListener(_SLL_BAIL_LISTENER)
    parser._errHandler = BailErrorStrategy()
    parser._interp.predictionMode = PredictionMode.SLL
    try:
        return getattr(parser, rule)()
    except Exception:
        # Any SLL hiccup (bail, prediction conflict, cancellation) -> fall back to LL. Broad on
        # purpose: the fast path must never change the outcome, only fail over to the slow one.
        return None


class _CollectingErrorListener(ErrorListener):
    """Best-effort mode: record every recovered syntax error (line, column) instead of raising.
    ANTLR's default error strategy then recovers and keeps parsing, so a single unparseable
    construct (a grammar gap) costs just that statement, not the whole file."""

    def __init__(self) -> None:
        self.errors: list[tuple[int, int]] = []

    def syntaxError(self, recognizer, offendingSymbol, line, column, msg, e):  # noqa: N802 (ANTLR API)
        self.errors.append((line, column))


def _parse(source: str, rule: str, on_failure_message: str, strict: bool = False, error_sink: list | None = None):
    """Shared parse-with-error-handling.

    Two-stage for speed (see _fast_parse): try SLL first; the slow, authoritative LL parse runs
    only when SLL couldn't decide.

    - Strict (fail-fast): the first LL error raises a positioned SqlSyntaxError for the caller to
      turn into a halt-with-diagnostics.
    - Best-effort (default): the LL parse uses ANTLR's default error *recovery* -- it never
      raises on a syntax error, it skips the offending construct and keeps going, returning a
      partial tree. Recovered errors are appended to `error_sink` (line, column) so the caller
      can report grammar gaps. This is what lets a real Oracle codebase ingest despite the
      vendored grammar's gaps (e.g. non-literal parameter defaults), losing only the individual
      constructs it can't parse rather than whole files."""
    if strict:
        # Fast SLL first; on a bail, the authoritative LL parse reports the error to halt.
        tree = _fast_parse(source, rule)
        if tree is not None:
            return tree
        listener = StrictHaltListener()
        try:
            lexer = PlSqlLexer(InputStream(source))
            lexer.removeErrorListeners()
            lexer.addErrorListener(listener)
            parser = PlSqlParser(CommonTokenStream(lexer))
            parser.removeErrorListeners()
            parser.addErrorListener(listener)
            parser._interp.predictionMode = PredictionMode.LL
            return getattr(parser, rule)()
        except Exception:
            raise  # fail fast -- the caller records diagnostics and stops

    # Best-effort: a single SLL pass with error *recovery*. SLL is near-linear, and recovery
    # skips (and records) the rare construct it can't handle -- structurally identical to LL on
    # everything SLL parses cleanly, but without LL's ~20x cost on this grammar's Python runtime.
    # Only if SLL genuinely can't proceed (throws) do we fall back to LL+recovery for that file.
    for mode in (PredictionMode.SLL, PredictionMode.LL):
        listener = _CollectingErrorListener()
        try:
            lexer = PlSqlLexer(InputStream(source))
            lexer.removeErrorListeners()
            lexer.addErrorListener(listener)
            parser = PlSqlParser(CommonTokenStream(lexer))
            parser.removeErrorListeners()
            parser.addErrorListener(listener)
            parser._interp.predictionMode = mode
            tree = getattr(parser, rule)()
        except Exception:
            if mode is PredictionMode.LL:  # even LL couldn't -- give up on this file
                logger.warning("%s (%d chars)", on_failure_message, len(source), exc_info=True)
                return None
            continue  # SLL threw -> retry once under LL
        if error_sink is not None:
            error_sink.extend(listener.errors)
        return tree
    return None


def parse_unit(source: str, rule: str = "sql_script", strict: bool = False) -> ParsedUnit:
    """Parses `source` (a full "CREATE OR REPLACE ..." unit, or a bare trigger body
    when rule="block") and extracts calls/table-refs. Never raises unless `strict`."""
    tree = _parse(source, rule, "Failed to parse PL/SQL source -- falling back to catalog-only dependencies", strict=strict)
    if tree is None:
        return ParsedUnit()

    visitor = _CallsAndTableRefsVisitor()
    visitor.visit(tree)
    return ParsedUnit(calls=visitor.calls, table_refs=visitor.table_refs)


def extract_package_member_sources(source: str) -> dict[tuple[str, tuple[str, ...]], str]:
    """Parses a full "CREATE OR REPLACE PACKAGE BODY ..." source and returns each
    member's own source, keyed by (member_name, parameter_data_types) -- see
    _PackageMemberSourceVisitor. Returns {} (never raises) if parsing fails."""
    tree = _parse(source, "sql_script", "Failed to parse package body -- members won't have their own source")
    if tree is None:
        return {}

    visitor = _PackageMemberSourceVisitor(source)
    visitor.visit(tree)
    return visitor.members


# -- Structural declarations (file-based ingestion) ---------------------------------
#
# The Oracle DB ingestor gets its structure (which packages/procedures/functions/
# parameters/triggers/types exist) from catalog ALL_* views. The file-based `plsql`
# ingestor has no database to ask, so it recovers that same structure from source
# by walking the parse tree here. Call/table edges reuse _CallsAndTableRefsVisitor
# unchanged -- run per-unit against each routine's own subtree so edges are attributed
# to the routine they occur in.


class _DeclarationVisitor(PlSqlParserVisitor):
    """Extracts package/procedure/function/parameter/trigger/type *declarations*
    from a parsed file into a RawPlsqlFile. Handled create_* rules don't recurse
    (their nested members are read directly), so nothing is double-counted; the
    default visitor still walks the file to reach every top-level unit."""

    def __init__(self, source: str, result: RawPlsqlFile) -> None:
        self._source = source
        self._result = result

    # Packages ----------------------------------------------------------------------

    def visitCreate_package(self, ctx):
        name = _last_identifier(_text(ctx.package_name()))
        members = []
        for obj in ctx.package_obj_spec():
            spec = obj.procedure_spec() or obj.function_spec()
            if spec is not None:
                members.append(self._routine_from_spec(spec))
        self._result.packages.append(RawPackage(name=name, spec_source=self._slice(ctx), members=members))

    def visitCreate_package_body(self, ctx):
        name = _last_identifier(_text(ctx.package_name()))
        members = []
        for obj in ctx.package_obj_body():
            body = obj.procedure_body() or obj.function_body()
            if body is not None:
                members.append(self._routine_from_body(body))
        self._result.packages.append(RawPackage(name=name, body_source=self._slice(ctx), members=members))

    # Standalone routines -------------------------------------------------------------

    def visitCreate_procedure_body(self, ctx):
        self._result.routines.append(
            self._routine(
                name=_last_identifier(_text(ctx.procedure_name())),
                kind="PROCEDURE",
                params_ctx=ctx,
                return_ctx=None,
                body_ctx=ctx,
            )
        )

    def visitCreate_function_body(self, ctx):
        self._result.routines.append(
            self._routine(
                name=_last_identifier(_text(ctx.function_name())),
                kind="FUNCTION",
                params_ctx=ctx,
                return_ctx=ctx,
                body_ctx=ctx,
            )
        )

    # Triggers ------------------------------------------------------------------------

    def visitCreate_trigger(self, ctx):
        body_ctx = ctx.trigger_body()
        calls, table_refs = _edges(body_ctx) if body_ctx is not None else ([], [])
        self._result.triggers.append(
            RawTrigger(
                name=_last_identifier(_text(ctx.trigger_name())),
                table=self._trigger_table(ctx),
                source=self._slice(ctx),
                calls=calls,
                table_refs=table_refs,
            )
        )

    # Types ---------------------------------------------------------------------------

    def visitCreate_type(self, ctx):
        definition = ctx.type_definition()
        if definition is None or definition.type_name() is None:
            return  # a bare TYPE BODY (no spec) -- named by its matching spec elsewhere
        kind = "OBJECT" if definition.object_type_def() is not None else None
        self._result.types.append(RawType(name=_last_identifier(_text(definition.type_name())), kind=kind))

    # SQL DDL -------------------------------------------------------------------------

    def visitCreate_table(self, ctx):
        relational = ctx.relational_table()
        if relational is None:
            return  # object/xmltype tables -- not modeled (rare; would need their own shape)
        table = RawTable(name=_last_identifier(_text(ctx.table_name())))
        pk = RawConstraint(name=None, type="PRIMARY_KEY", columns=[])
        position = 0
        for prop in relational.relational_property():
            column_def = prop.column_definition()
            if column_def is not None:
                position += 1
                table.columns.append(self._column(column_def, position, table, pk))
                continue
            out_of_line = prop.out_of_line_constraint()
            if out_of_line is not None:
                self._out_of_line_constraint(out_of_line, table, pk)
        pk_set = {c.upper() for c in pk.columns}
        for column in table.columns:
            if column.name.upper() in pk_set:
                column.is_primary_key = True
        if pk.columns:
            table.constraints.insert(0, pk)
        self._result.tables.append(table)

    def _column(self, ctx, position: int, table: RawTable, pk: RawConstraint) -> RawColumn:
        name = _last_identifier(_text(ctx.column_name()))
        datatype = ctx.datatype()
        data_type = datatype.getText() if datatype is not None else (_text(ctx.type_name()))
        not_null = False
        for inline in ctx.inline_constraint():
            if inline.PRIMARY():
                pk.columns.append(name)
                pk.name = pk.name or _constraint_name(inline)
            if inline.NOT() and inline.NULL_():
                not_null = True
            if inline.UNIQUE():
                table.constraints.append(RawConstraint(name=_constraint_name(inline), type="UNIQUE", columns=[name]))
            check = inline.check_constraint()
            if check is not None:
                table.constraints.append(
                    RawConstraint(name=_constraint_name(inline), type="CHECK", expression=check.condition().getText())
                )
            ref = inline.references_clause()
            if ref is not None:
                table.foreign_keys.append(self._foreign_key(_constraint_name(inline), [name], ref))
        return RawColumn(
            name=name,
            position=position,
            data_type=data_type,
            nullable=not not_null,
            default_value=ctx.expression().getText() if ctx.expression() else None,
        )

    def _out_of_line_constraint(self, ctx, table: RawTable, pk: RawConstraint) -> None:
        name = _constraint_name(ctx)
        columns = [_last_identifier(c.getText()) for c in ctx.column_name()]
        fk = ctx.foreign_key_clause()
        if fk is not None:
            constrained = [_last_identifier(c.getText()) for c in _column_names(fk.paren_column_list())]
            table.foreign_keys.append(self._foreign_key(name, constrained, fk.references_clause()))
        elif ctx.PRIMARY():
            pk.columns.extend(columns)
            pk.name = pk.name or name
        elif ctx.UNIQUE():
            table.constraints.append(RawConstraint(name=name, type="UNIQUE", columns=columns))
        elif ctx.CHECK() and ctx.condition() is not None:
            table.constraints.append(RawConstraint(name=name, type="CHECK", expression=ctx.condition().getText()))

    @staticmethod
    def _foreign_key(name, columns, references_clause) -> RawForeignKey:
        ref_columns = [_last_identifier(c.getText()) for c in _column_names(references_clause.paren_column_list())]
        return RawForeignKey(
            name=name,
            columns=columns,
            ref_table=_last_identifier(_text(references_clause.tableview_name())),
            ref_columns=ref_columns,
        )

    def visitAlter_table(self, ctx):
        # `ALTER TABLE t ADD CONSTRAINT ... (FK/PK/UNIQUE/CHECK)` and `ALTER TABLE t
        # ADD (columns)` -- reuses the same constraint/column helpers as CREATE TABLE,
        # collecting the additions to merge onto the by-name table in resolution.
        name = _last_identifier(_text(ctx.tableview_name()))
        scratch = RawTable(name=name)
        pk = RawConstraint(name=None, type="PRIMARY_KEY", columns=[])

        for clause in _as_list(ctx.constraint_clauses()):
            for out_of_line in clause.out_of_line_constraint():
                self._out_of_line_constraint(out_of_line, scratch, pk)

        position = 0
        for clause in _as_list(ctx.column_clauses()):
            add_modify = clause.add_modify_drop_column_clauses()
            if add_modify is None:
                continue
            for add_clause in _as_list(add_modify.add_column_clause()):
                for column_def in add_clause.column_definition():
                    position += 1
                    scratch.columns.append(self._column(column_def, position, scratch, pk))

        if pk.columns:
            pk_set = {c.upper() for c in pk.columns}
            for column in scratch.columns:
                if column.name.upper() in pk_set:
                    column.is_primary_key = True
            scratch.constraints.insert(0, pk)

        if scratch.columns or scratch.constraints or scratch.foreign_keys:
            self._result.alterations.append(
                RawTableAlteration(name, scratch.columns, scratch.constraints, scratch.foreign_keys)
            )

    def visitCreate_view(self, ctx):
        name = _last_identifier(_text(ctx.id_expression()))
        select = ctx.select_only_statement()
        reads: list[str] = []
        if select is not None:
            _, table_refs = _edges(select)
            reads = list(dict.fromkeys(name for name, _ in table_refs))  # unique, order-preserving
        self._result.views.append(RawView(name=name, definition=self._slice(ctx), reads=reads))

    def visitCreate_index(self, ctx):
        clause = ctx.table_index_clause()
        if clause is None:
            return  # cluster/bitmap-join index -- not a plain table index
        self._result.indexes.append(
            RawIndexDef(
                name=_last_identifier(_text(ctx.index_name())),
                table=_last_identifier(_text(clause.tableview_name())),
                columns=[_last_identifier(e.getText()) for e in clause.index_expr()],
                unique=bool(ctx.UNIQUE()),
            )
        )

    def visitCreate_sequence(self, ctx):
        self._result.sequences.append(RawSequence(name=_last_identifier(_text(ctx.sequence_name()))))

    # Helpers -------------------------------------------------------------------------

    def _routine_from_spec(self, spec) -> RawRoutine:
        # A spec (in a package spec / .pks) is a signature only -- no body, so no
        # source or call/table edges; those come from the matching body member.
        kind = "FUNCTION" if type(spec).__name__ == "Function_specContext" else "PROCEDURE"
        return RawRoutine(
            name=_text(spec.identifier()).upper(),
            kind=kind,
            parameters=self._params(spec),
            return_type=self._return_type(spec) if kind == "FUNCTION" else None,
        )

    def _routine_from_body(self, body) -> RawRoutine:
        kind = "FUNCTION" if type(body).__name__ == "Function_bodyContext" else "PROCEDURE"
        return self._routine(
            name=_text(body.identifier()).upper(),
            kind=kind,
            params_ctx=body,
            return_ctx=body if kind == "FUNCTION" else None,
            body_ctx=body,
        )

    def _routine(self, name, kind, params_ctx, return_ctx, body_ctx) -> RawRoutine:
        calls, table_refs = _edges(body_ctx) if body_ctx is not None else ([], [])
        return RawRoutine(
            name=name,
            kind=kind,
            parameters=self._params(params_ctx),
            return_type=self._return_type(return_ctx) if return_ctx is not None else None,
            source=self._slice(body_ctx) if body_ctx is not None else None,
            calls=calls,
            table_refs=table_refs,
        )

    def _params(self, ctx) -> list[RawParameter]:
        params = []
        for position, p in enumerate(ctx.parameter(), start=1):
            params.append(
                RawParameter(
                    name=p.parameter_name().getText() if p.parameter_name() else None,
                    position=position,
                    direction=_direction(p),
                    data_type=p.type_spec().getText() if p.type_spec() else None,
                    has_default=p.default_value_part() is not None,
                )
            )
        return params

    @staticmethod
    def _return_type(ctx) -> str | None:
        type_spec = ctx.type_spec()
        return type_spec.getText() if type_spec is not None else None

    @staticmethod
    def _trigger_table(ctx) -> str | None:
        simple = ctx.simple_dml_trigger()
        if simple is None:
            return None
        event = simple.dml_event_clause()
        if event is None or event.tableview_name() is None:
            return None
        return _last_identifier(_text(event.tableview_name()))

    def _slice(self, ctx) -> str:
        return self._source[ctx.start.start : ctx.stop.stop + 1]


def _direction(param_ctx) -> str | None:
    # IN/OUT are matched in a `(IN | OUT | ...)*` repetition, so these token
    # accessors return (possibly empty) lists, not single nodes -- test truthiness,
    # not `is not None` (an empty list is not None but means "absent").
    has_in = bool(param_ctx.IN())
    has_out = bool(param_ctx.OUT())
    if has_in and has_out:
        return "IN OUT"
    if has_out:
        return "OUT"
    if has_in:
        return "IN"
    return None  # unspecified -- Oracle defaults to IN, left implicit rather than guessed


def _as_list(node_or_list):
    """An ANTLR accessor for a rule of variable cardinality returns a single context,
    a list, or None depending on how it matched -- normalize to a list."""
    if node_or_list is None:
        return []
    return node_or_list if isinstance(node_or_list, list) else [node_or_list]


def _constraint_name(ctx) -> str | None:
    """A constraint's `CONSTRAINT <name>` label if present (both inline and
    out-of-line constraints make it optional -- unnamed ones get a positional name
    assigned later in resolution, mirroring the SQL ingestor)."""
    name_ctx = ctx.constraint_name()
    return _last_identifier(name_ctx.getText()) if name_ctx is not None else None


def _column_names(paren_column_list) -> list:
    """The column_name contexts inside a `(col, col, ...)` list, tolerating a
    missing list."""
    if paren_column_list is None:
        return []
    column_list = paren_column_list.column_list()
    return column_list.column_name() if column_list is not None else []


def _edges(ctx) -> tuple[list[str], list[tuple[str, str]]]:
    """Calls + (table, operation) pairs occurring anywhere under `ctx`, via the same
    _CallsAndTableRefsVisitor the catalog ingestor uses -- run against a subtree here
    rather than a re-parsed string, so no work is duplicated."""
    visitor = _CallsAndTableRefsVisitor()
    visitor.visit(ctx)
    return visitor.calls, [(ref.name, ref.operation) for ref in visitor.table_refs]


# Top-level statement kinds (unit_statement child rule names, "Context" stripped) the
# _DeclarationVisitor turns into graph nodes. Anything the grammar parses that ISN'T here
# (CREATE MATERIALIZED VIEW / SYNONYM, COMMENT ON, GRANT, CREATE USER, ...) is a coverage
# gap -- reported by the strict path rather than silently dropped. Keep in sync with the
# visitCreate_*/visitAlter_* methods above.
MODELED_STATEMENT_KINDS = frozenset(
    {
        "Create_table",
        "Alter_table",
        "Create_view",
        "Create_index",
        "Create_sequence",
        "Create_package",
        "Create_package_body",
        "Create_procedure_body",
        "Create_function_body",
        "Create_trigger",
        "Create_type",
    }
)


def _statement_kinds(tree) -> list[str]:
    """The top-level statement kind of each unit_statement in a parsed sql_script, e.g.
    ["Create_table", "Create_materialized_view"]. Best-effort; never raises."""
    kinds: list[str] = []
    for child in getattr(tree, "children", None) or []:
        if type(child).__name__ != "Unit_statementContext":
            continue
        inner = next(iter(getattr(child, "children", None) or []), None)
        if inner is not None:
            kinds.append(type(inner).__name__.removesuffix("Context"))
    return kinds


def unmodeled_statement_kinds(raw_file: RawPlsqlFile) -> list[str]:
    """Statement kinds this file contained that the ingestor parsed but does not model as
    graph nodes -- so the run can log coverage gaps instead of silently dropping them."""
    return [k for k in raw_file.statement_kinds if k not in MODELED_STATEMENT_KINDS]


# SQL*Plus directives are line-oriented commands (no `;`/`/` terminator) that wrap real DDL in
# Oracle deploy scripts -- `prompt`, `set`, `spool`, `define`, `whenever`, `@`, `REM`, etc. The
# vendored PL/SQL grammar can't place them between statements (it lexes `prompt ...` as one
# token but then expects a terminator), so a script like `prompt ...\nCREATE INDEX ...` fails at
# the CREATE. We blank these lines before parsing. Only line-leading keywords that are NOT also
# SQL statement/continuation leaders are stripped; `SET` is stripped only when the line has no
# `=` (so an `UPDATE ... \n SET col = val` continuation is never touched).
# Pure SQL*Plus line commands -- none of these can begin a SQL/PL-SQL statement or clause, so
# a whole line starting with one is safe to blank.
_SQLPLUS_LEADERS = (
    "prompt", "spool", "whenever", "define", "undefine", "def", "undef",
    "ttitle", "btitle", "repheader", "repfooter", "accept", "pause", "variable",
    "show", "host", "clear", "store", "save", "print", "column", "exec",
    "rem", "remark", "disconnect", "quit", "break", "compute", "timing",
)
# SQL*Plus commands whose keyword ALSO begins a real SQL clause -- blanked only when it's NOT
# the SQL form (negative lookahead). This is the crux: `START dropdatalayer` (run a script) vs
# `START WITH` (hierarchical query); `CONNECT scott/tiger` vs `CONNECT BY`; SQL*Plus
# `EXECUTE some_proc` vs PL/SQL `EXECUTE IMMEDIATE`. Blank the command, keep the clause.
# (`exec` -- the short form -- is a pure SQL*Plus leader above, since PL/SQL always spells out
# EXECUTE IMMEDIATE.) `exit` is deliberately absent: it collides with PL/SQL `EXIT`/`EXIT WHEN`
# and can't be disambiguated by a line prefix.
_SQLPLUS_CONDITIONAL = (
    r"start\b(?!\s+with\b)",
    r"connect\b(?!\s+by\b)",
    r"execute\b(?!\s+immediate\b)",
)
# SQL*Plus `SET <option>` -- allowlisted options only, so real SQL keeps working: `SET
# TRANSACTION`, `SET ROLE`, `SET CONSTRAINTS`, and an `UPDATE ... SET col = ...` continuation
# all fall through untouched (none of their next tokens are in this list).
_SQLPLUS_SET_OPTIONS = (
    "define", "serveroutput", "echo", "feedback", "heading", "pagesize", "pages",
    "linesize", "lines", "verify", "termout", "trimspool", "trimout", "timing", "time",
    "sqlblanklines", "scan", "escape", "concat", "colsep", "null", "newpage", "space",
    "recsep", "underline", "wrap", "long", "longchunksize", "arraysize", "autocommit",
    "autotrace", "autoprint", "showmode", "sqlprompt", "markup", "serverout", "flush",
    "embedded", "numwidth", "numformat", "cmdsep", "appinfo", "sqlterminator", "pause",
)
_SQLPLUS_RE = re.compile(
    r"^\s*(?:@{1,2}"
    r"|(?:" + "|".join(_SQLPLUS_LEADERS) + r")\b"
    r"|set\s+(?:" + "|".join(_SQLPLUS_SET_OPTIONS) + r")\b"
    r"|" + "|".join(_SQLPLUS_CONDITIONAL) + r")",
    re.IGNORECASE,
)


def _blank_sqlplus_directives(source: str) -> str:
    """Replace each SQL*Plus directive line's content with equal-length spaces (newline kept),
    so the parser sees only real SQL/PL-SQL while every byte offset, line, and column stays
    identical -- diagnostics positions and stored entity `source` slices remain exact."""
    out: list[str] = []
    for line in source.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        newline = line[len(body):]
        if _SQLPLUS_RE.match(body):
            out.append(" " * len(body) + newline)
        else:
            out.append(line)
    return "".join(out)


# -- Top-level INSERT (data-population) stripping ------------------------------------
#
# A real Oracle export is frequently hundreds of files of nothing but `INSERT INTO ...
# VALUES (...);` -- data population. The ingestor models schema (CREATE TABLE / VIEW /
# PROCEDURE / ...), never row data: a top-level INSERT parses to an `Insert_statement`
# unit the _DeclarationVisitor doesn't model, so it contributes ZERO graph nodes/edges --
# yet the grammar still has to fully lex/parse it. On big data dumps that wasted parse is
# exactly what makes the ingestor choke (tens of seconds/file, and the ANTLR prediction-DFA
# cache grows until a worker OOMs). Blanking these statements before parsing removes the
# wasted work while leaving graph output byte-for-byte identical.
#
# Crucially only *top-level* INSERTs (PL/SQL block depth 0) are removed. An INSERT *inside*
# a procedure/function/trigger/anonymous block is application logic -- it becomes the INSERT
# table-operation edge the ingestor DOES model (RawRoutine.table_refs) -- and must survive.
# The word "INSERT" that is not a statement leader (a trigger header's `BEFORE INSERT ON t`,
# an `INSERT` inside a stripped statement's own text) is never mistaken for one.
_BLOCK_OPENERS = frozenset({"BEGIN", "IF", "LOOP", "CASE"})
# `END IF` / `END LOOP` / `END CASE` each close ONE opener (the IF/LOOP/CASE), so the trailing
# keyword is consumed as part of the END rather than counted as its own opener.
_END_COMPANIONS = frozenset({"IF", "LOOP", "CASE"})
_NON_NEWLINE = re.compile(r"[^\r\n]")
_INSERT_PROBE = re.compile(r"insert", re.IGNORECASE)  # short-circuits; no full lowercased copy


def _skip_quotes_and_comments(source: str, i: int, n: int) -> int | None:
    """If `source[i]` begins a string literal (ordinary or Oracle `q'...'` quoting), a quoted
    identifier, a `--` line comment, or a `/* */` block comment, return the index just past it;
    otherwise None. Shared skip logic so INSERT-stripping never mistakes a `;`, or the word
    INSERT, inside a string/comment for real SQL. (`/` is treated as a comment only as `/*`;
    a lone `/` is the SQL*Plus terminator, handled by the caller.)"""
    c = source[i]
    if c in "qQ" and i + 2 < n and source[i + 1] == "'":  # Oracle q'[..]' q'{..}' q'(..)' q'<..>' q'x..x'
        opener = source[i + 2]
        closer = {"[": "]", "{": "}", "(": ")", "<": ">"}.get(opener, opener)
        end = source.find(closer + "'", i + 3)
        return end + 2 if end != -1 else n
    if c == "'":  # ordinary string literal ('' escapes an embedded quote)
        j = i + 1
        while j < n:
            if source[j] == "'":
                if j + 1 < n and source[j + 1] == "'":
                    j += 2
                    continue
                return j + 1
            j += 1
        return n
    if c == '"':  # quoted identifier -- may legally contain ';'
        end = source.find('"', i + 1)
        return end + 1 if end != -1 else n
    if c == "-" and i + 1 < n and source[i + 1] == "-":  # line comment
        j = source.find("\n", i)
        return n if j == -1 else j
    if c == "/" and i + 1 < n and source[i + 1] == "*":  # block comment
        j = source.find("*/", i + 2)
        return n if j == -1 else j + 2
    return None


def _peek_word(source: str, j: int, n: int) -> tuple[str | None, int]:
    """Skip whitespace from `j`, then read one identifier word. Returns (WORD_UPPER, index_past)
    or (None, j') if the next non-space thing isn't a word -- used to swallow an `END`'s
    IF/LOOP/CASE companion."""
    while j < n and source[j].isspace():
        j += 1
    if j < n and (source[j].isalpha() or source[j] == "_"):
        k = j + 1
        while k < n and (source[k].isalnum() or source[k] in "_$#"):
            k += 1
        return source[j:k].upper(), k
    return None, j


def _strip_top_level_inserts(source: str) -> str:
    """Blank every top-level INSERT statement (data population) length- and line-preservingly,
    so the parser never spends time on it. See the block comment above for why.

    Correctness rests on one invariant: every INSERT that is application logic lives inside a
    `BEGIN ... END` block, so at the point we reach it the enclosing BEGIN has already raised
    `depth` to >= 1 and we leave it untouched. `depth` is tracked coarsely (openers/END, with
    `END IF/LOOP/CASE` companions folded in) and clamped at 0; any drift only ever pushes depth
    to 0 in a *declaration* scope where no executable INSERT can appear, and a lone `/`
    (SQL*Plus terminator) resyncs depth to 0 at every unit boundary -- so an embedded INSERT is
    never stripped, while an over-count merely forgoes the speed-up on some data INSERT.

    Every byte offset, line and column is preserved (statements overwritten with spaces,
    newlines kept), so diagnostics positions and the `source` slices stored for surrounding
    real objects stay exact -- exactly like _blank_sqlplus_directives / _neutralize_param_defaults."""
    if not _INSERT_PROBE.search(source):  # fast path: nothing to do (the common code-only file)
        return source
    out: list[str] = []
    last = 0  # start of the not-yet-emitted verbatim region
    i, n, depth = 0, len(source), 0
    at_stmt_start = True
    while i < n:
        skipped = _skip_quotes_and_comments(source, i, n)
        if skipped is not None:  # string / comment -- copied verbatim, never a statement boundary
            i = skipped
            continue
        c = source[i]
        if c.isspace():  # whitespace never changes statement-start state
            i += 1
            continue
        if c == ";":
            if depth == 0:
                at_stmt_start = True
            i += 1
            continue
        if c == "/" and _is_terminator_slash(source, i):
            depth = 0  # SQL*Plus terminator: force-ends the current unit -> resync to top level
            at_stmt_start = True
            i += 1
            continue
        if c.isalpha() or c == "_":  # an identifier / keyword
            j = i + 1
            while j < n and (source[j].isalnum() or source[j] in "_$#"):
                j += 1
            word = source[i:j].upper()
            if word == "END":
                depth = max(0, depth - 1)
                companion, companion_end = _peek_word(source, j, n)
                if companion in _END_COMPANIONS:
                    j = companion_end
                at_stmt_start = False
                i = j
                continue
            if word in _BLOCK_OPENERS:
                depth += 1
                at_stmt_start = False
                i = j
                continue
            if word == "INSERT" and at_stmt_start and depth == 0:
                end = _find_statement_end(source, j, n)  # index just past the terminating ';' (or EOF)
                out.append(source[last:i])
                out.append(_NON_NEWLINE.sub(" ", source[i:end]))  # length/newline-preserving blank
                last = end
                i = end
                at_stmt_start = True
                continue
            at_stmt_start = False
            i = j
            continue
        # any other punctuation (',', '(', operators, ...) -- we're inside a statement now
        at_stmt_start = False
        i += 1
    out.append(source[last:])
    return "".join(out)


def _is_terminator_slash(source: str, i: int) -> bool:
    """A lone `/` on its own line is the SQL*Plus run-buffer terminator (vs division `a/b`).
    True when everything from the previous newline to `i`, and from `i+1` to the next newline,
    is whitespace."""
    start = source.rfind("\n", 0, i) + 1
    if source[start:i].strip():
        return False
    end = source.find("\n", i + 1)
    tail = source[i + 1:] if end == -1 else source[i + 1:end]
    return not tail.strip()


def _find_statement_end(source: str, i: int, n: int) -> int:
    """Index just past the `;` that terminates the INSERT beginning before `i` -- skipping over
    string/comment content so a `;` inside `VALUES ('a;b')` isn't mistaken for it. Returns `n`
    (EOF) if the statement is unterminated (a trailing INSERT in a data file)."""
    while i < n:
        skipped = _skip_quotes_and_comments(source, i, n)
        if skipped is not None:
            i = skipped
            continue
        if source[i] == ";":
            return i + 1
        i += 1
    return n


def _neutralize_param_defaults(source: str) -> str:
    """Replace parameter/record/cursor default *expressions* (`:= <expr>` inside a paren group)
    with a literal `0`, length-preserved, so the grammar parses them -- the vendored PL/SQL
    grammar only allows a literal `constant` as a default, but Oracle allows any expression
    (`p objUser := objUser()`, `p number := sysdate`, `p date := f(x)`). We only model
    `has_default` (a bool), never the default *value*, so nothing is lost; the parameter itself
    (name, type, has-default) is still extracted, and the file parses instead of halting.

    Only `:=` at paren depth >= 1 is touched -- i.e. inside a signature's/record's/cursor's
    `(...)`. Body assignments (`v := expr;`, always at paren depth 0) are left untouched. Strings
    (incl. Oracle `q'[...]'` quoting) and comments are skipped so their contents never affect the
    paren tracking. Length is preserved (the expression is overwritten with `0` + spaces) so
    every byte offset, line, and column -- and diagnostics/stored source -- stay exact."""
    out: list[str] = []
    i, n, depth = 0, len(source), 0
    while i < n:
        c = source[i]
        # Oracle alternative quoting: q'[...]' q'{...}' q'(...)' q'<...>' q'x...x'
        if c in "qQ" and i + 2 < n and source[i + 1] == "'":
            opener = source[i + 2]
            closer = {"[": "]", "{": "}", "(": ")", "<": ">"}.get(opener, opener)
            end = source.find(closer + "'", i + 3)
            if end != -1:
                out.append(source[i:end + 2]); i = end + 2; continue
        if c == "'":  # ordinary string literal ('' escapes an embedded quote)
            j = i + 1
            while j < n:
                if source[j] == "'":
                    if j + 1 < n and source[j + 1] == "'":
                        j += 2; continue
                    j += 1; break
                j += 1
            out.append(source[i:j]); i = j; continue
        if c == "-" and i + 1 < n and source[i + 1] == "-":  # line comment
            j = source.find("\n", i); j = n if j == -1 else j
            out.append(source[i:j]); i = j; continue
        if c == "/" and i + 1 < n and source[i + 1] == "*":  # block comment
            j = source.find("*/", i + 2); j = n if j == -1 else j + 2
            out.append(source[i:j]); i = j; continue
        if c == "(":
            depth += 1; out.append(c); i += 1; continue
        if c == ")":
            depth -= 1; out.append(c); i += 1; continue
        if depth >= 1 and c == ":" and i + 1 < n and source[i + 1] == "=":
            out.append(":=")
            i += 2
            start, d = i, 0  # scan the default expr to the next ',' or ')' at THIS paren level
            while i < n:
                ch = source[i]
                if ch in "qQ" and i + 2 < n and source[i + 1] == "'":
                    opener = source[i + 2]
                    closer = {"[": "]", "{": "}", "(": ")", "<": ">"}.get(opener, opener)
                    end = source.find(closer + "'", i + 3)
                    if end != -1:
                        i = end + 2; continue
                if ch == "'":
                    k = i + 1
                    while k < n:
                        if source[k] == "'":
                            if k + 1 < n and source[k + 1] == "'":
                                k += 2; continue
                            k += 1; break
                        k += 1
                    i = k; continue
                if ch == "-" and i + 1 < n and source[i + 1] == "-":
                    j = source.find("\n", i); i = n if j == -1 else j; continue
                if ch == "(":
                    d += 1; i += 1; continue
                if ch == ")":
                    if d == 0:
                        break  # closes the param-list group -> end of this default
                    d -= 1; i += 1; continue
                if d == 0 and ch in ",;":
                    break  # next param (,) -- or a stray ; (bail, don't over-eat)
                i += 1
            expr = source[start:i]
            out.append("0".ljust(len(expr)) if expr else "")  # length-preserving literal
            continue
        out.append(c); i += 1
    return "".join(out)


# -- Compiled-accelerator fast path (JSON -> RawPlsqlFile) ---------------------------
# The C++ accelerator returns the same data these dataclasses hold, as JSON; rebuild the Raw* model
# from it so the rest of the pipeline (resolution.py) is identical to the pure-Python path.


def _routine_from_json(r: dict) -> RawRoutine:
    return RawRoutine(
        name=r["name"],
        kind=r["kind"],
        parameters=[
            RawParameter(
                name=p["name"], position=p["position"], direction=p["direction"],
                data_type=p["data_type"], has_default=p["has_default"],
            )
            for p in r["parameters"]
        ],
        return_type=r["return_type"],
        source=r["source"],
        calls=list(r["calls"]),
        table_refs=[(name, op) for name, op in r["table_refs"]],
    )


def _column_from_json(c: dict) -> RawColumn:
    return RawColumn(
        name=c["name"], position=c["position"], data_type=c["data_type"],
        nullable=c["nullable"], default_value=c["default_value"], is_primary_key=c["is_primary_key"],
    )


def _constraint_from_json(c: dict) -> RawConstraint:
    return RawConstraint(name=c["name"], type=c["type"], columns=list(c["columns"]), expression=c["expression"])


def _foreign_key_from_json(f: dict) -> RawForeignKey:
    return RawForeignKey(
        name=f["name"], columns=list(f["columns"]), ref_table=f["ref_table"], ref_columns=list(f["ref_columns"])
    )


def _table_from_json(t: dict) -> RawTable:
    return RawTable(
        name=t["name"],
        columns=[_column_from_json(c) for c in t["columns"]],
        constraints=[_constraint_from_json(c) for c in t["constraints"]],
        foreign_keys=[_foreign_key_from_json(f) for f in t["foreign_keys"]],
    )


def _build_raw_from_json(data: dict, path: str) -> RawPlsqlFile:
    result = RawPlsqlFile(path=path)
    result.statement_kinds = list(data.get("statement_kinds") or [])
    for p in data.get("packages") or []:
        result.packages.append(
            RawPackage(
                name=p["name"], spec_source=p["spec_source"], body_source=p["body_source"],
                members=[_routine_from_json(m) for m in p["members"]],
            )
        )
    result.routines.extend(_routine_from_json(r) for r in data.get("routines") or [])
    for t in data.get("triggers") or []:
        result.triggers.append(
            RawTrigger(
                name=t["name"], table=t["table"], source=t["source"],
                calls=list(t["calls"]), table_refs=[(name, op) for name, op in t["table_refs"]],
            )
        )
    result.types.extend(RawType(name=t["name"], kind=t["kind"]) for t in data.get("types") or [])
    result.tables.extend(_table_from_json(t) for t in data.get("tables") or [])
    result.views.extend(
        RawView(name=v["name"], definition=v["definition"], reads=list(v["reads"]))
        for v in data.get("views") or []
    )
    result.indexes.extend(
        RawIndexDef(name=i["name"], table=i["table"], columns=list(i["columns"]), unique=i["unique"])
        for i in data.get("indexes") or []
    )
    result.sequences.extend(RawSequence(name=s["name"]) for s in data.get("sequences") or [])
    for a in data.get("alterations") or []:
        result.alterations.append(
            RawTableAlteration(
                table=a["table"],
                columns=[_column_from_json(c) for c in a["columns"]],
                constraints=[_constraint_from_json(c) for c in a["constraints"]],
                foreign_keys=[_foreign_key_from_json(f) for f in a["foreign_keys"]],
            )
        )
    return result


def _accel_extract(source: str, path: str, original_lines: list[str]) -> RawPlsqlFile | None:
    """Best-effort parse+extract via the C++ accelerator: one call returns the whole RawPlsqlFile as
    JSON (tree stays in C++). Returns None on any failure so the caller falls back to pure Python.
    Recovered syntax errors are turned into the same parse_errors/first_error/error_samples the
    Python path reports."""
    try:
        data = json.loads(_accel.extract(source))
    except Exception:
        logger.warning("accelerator extract failed; retrying in pure Python (%d chars)", len(source))
        return None
    result = _build_raw_from_json(data, path)
    errors = data.get("errors") or []
    if errors:
        result.parse_errors = len(errors)
        result.first_error = (errors[0][0], errors[0][1])
        seen: set[int] = set()
        for line, _col in errors:
            if line in seen:
                continue
            seen.add(line)
            result.error_samples.append(
                (line, original_lines[line - 1].strip()[:200] if 0 < line <= len(original_lines) else "")
            )
            if len(result.error_samples) >= 25:
                break
    return result


# -- Leading documentation comments (`doc`) -----------------------------------------
#
# A PL/SQL object's doc comment is the contiguous `--` line block or the `/* ... */` block
# immediately preceding its `CREATE ...` (or, for a packaged member, its PROCEDURE/FUNCTION
# declaration inside the package body), with no blank-line gap. We attach it in ONE
# parser-agnostic post-pass over the (already transformed) file text rather than inside the
# parse tree walk: the vendored ANTLR grammar discards inter-statement comments, and the
# optional C++ accelerator returns no positional info at all -- but both hand back each
# object's own verbatim `source` slice, so we relocate that slice in the file text and read
# the comment above it. Running the SAME pass on both the pure-Python and accelerator outputs
# keeps them byte-identical (the accelerator-parity contract). The three pre-parse transforms
# (_blank_sqlplus_directives / _strip_top_level_inserts / _neutralize_param_defaults) all
# preserve comments verbatim and every byte offset, so scanning the transformed source is exact.


def _leading_doc(source: str, start_line: int) -> str:
    """The documentation comment immediately above 1-based `start_line`, cleaned, or "".

    A contiguous run of `--` line comments (via the shared leading_comment_from_source), or,
    failing that, a `/* ... */` block whose closing `*/` sits on the line directly above. A
    blank line between the comment and the declaration means it is not a doc comment (both
    branches stop at the gap), matching the tree-sitter/AST engines' rule."""
    doc = leading_comment_from_source(source, start_line, prefixes=("--",))
    if doc:
        return doc
    lines = source.splitlines()
    i = start_line - 2  # 0-based index of the line directly above the declaration
    if i < 0 or not lines[i].strip().endswith("*/"):
        return ""  # no block comment butting up against the declaration
    block: list[str] = []
    while i >= 0:
        block.append(lines[i])
        if "/*" in lines[i]:  # found the block's opener -> we have the whole comment
            block.reverse()
            return clean_comment("\n".join(block))
        i -= 1
    return ""  # ran off the top without an opener -- not a well-formed block comment


def _line_of(source: str, needle: str) -> int | None:
    """The 1-based line where `needle` (an object's own verbatim source slice) begins in
    `source`, or None if absent. The slice is the whole CREATE...END span, so it is
    effectively unique -- collisions are possible only between byte-identical overloads."""
    idx = source.find(needle)
    return None if idx < 0 else source.count("\n", 0, idx) + 1


def _attach_docs(result: RawPlsqlFile, source: str) -> None:
    """Set `.doc` on every routine/package/trigger in `result` by locating each object's own
    source slice in the (transformed) file `source` and reading the comment block above it.
    Parser-agnostic (see the section comment): runs identically on the pure-Python and C++
    accelerator outputs, so the two stay byte-for-byte in parity. Package-spec members have no
    source of their own (they are signatures) and so are left with doc="" -- a body member, or a
    standalone routine, carries the doc."""
    def doc_for(anchor: str | None) -> str:
        if not anchor:
            return ""
        line = _line_of(source, anchor)
        return _leading_doc(source, line) if line is not None else ""

    for pkg in result.packages:
        pkg.doc = doc_for(pkg.spec_source or pkg.body_source)
        for member in pkg.members:
            member.doc = doc_for(member.source)
    for routine in result.routines:
        routine.doc = doc_for(routine.source)
    for trigger in result.triggers:
        trigger.doc = doc_for(trigger.source)


def parse_declarations(source: str, path: str = "", strict: bool = False) -> RawPlsqlFile:
    """Parse one PL/SQL source file into its structural declarations (packages,
    standalone routines, triggers, types, SQL DDL) plus each routine's call/table edges.
    Default: never raises -- a file that fails to parse yields an empty RawPlsqlFile and a
    logged warning. Strict: the first parse error raises a positioned SqlSyntaxError (see
    _parse) for the caller to turn into a halt-with-diagnostics.

    SQL*Plus directive lines (prompt/set/spool/@/...) are blanked first -- see
    _blank_sqlplus_directives -- since the grammar is PL/SQL, not the SQL*Plus script format
    that wraps it in Oracle deploy scripts."""
    result = RawPlsqlFile(path=path)
    original_lines = source.splitlines()  # for gap samples -- transforms below are line-preserving
    # Strip data-population INSERTs first (biggest saving, on the SQL*Plus-cleaned source), then
    # neutralise param defaults on what's left. All three transforms are byte-offset/line preserving.
    # Set AURYN_PLSQL_KEEP_INSERTS=1 to opt out (e.g. to A/B the speed-up, or if row data is
    # ever wanted); default is to skip them, since the ingestor models schema, not row data.
    source = _blank_sqlplus_directives(source)
    if os.environ.get("AURYN_PLSQL_KEEP_INSERTS") != "1":
        source = _strip_top_level_inserts(source)
    source = _neutralize_param_defaults(source)

    # Fast path: the C++ accelerator parses AND extracts in one call (best-effort only). Any failure
    # falls through to the pure-Python parse below, so behaviour is identical with or without it.
    if _accel is not None and not strict:
        accelerated = _accel_extract(source, path, original_lines)
        if accelerated is not None:
            _attach_docs(accelerated, source)  # doc capture is Python-side (the C++ tree has no comments)
            return accelerated

    errors: list[tuple[int, int]] = []
    tree = _parse(
        source, "sql_script", f"Failed to parse PL/SQL source {path!r} -- no declarations extracted",
        strict=strict, error_sink=errors,
    )
    if tree is None:
        return result
    if errors:  # best-effort recovery skipped some construct(s) -- a grammar gap, but the rest parsed
        result.parse_errors = len(errors)
        result.first_error = errors[0]
        # One sample per distinct line (the offending source), capped -- so the grammar-gaps
        # report shows the actual constructs, enough to fix each class without a re-run.
        seen: set[int] = set()
        for line, _col in errors:
            if line in seen:
                continue
            seen.add(line)
            text = original_lines[line - 1].strip()[:200] if 0 < line <= len(original_lines) else ""
            result.error_samples.append((line, text))
            if len(result.error_samples) >= 25:
                break
    result.statement_kinds = _statement_kinds(tree)
    _DeclarationVisitor(source, result).visit(tree)
    _attach_docs(result, source)
    return result


__all__ = [
    "MODELED_STATEMENT_KINDS",
    "ParsedUnit",
    "accelerator_available",
    "SqlSyntaxError",
    "TableRef",
    "extract_package_member_sources",
    "parse_declarations",
    "parse_unit",
    "unmodeled_statement_kinds",
]
